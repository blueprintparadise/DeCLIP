import logging
from collections import OrderedDict
import math
from typing import Callable, Optional, Sequence, Tuple
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.checkpoint import checkpoint
from torchvision.ops import roi_align
from .utils import to_2tuple


class LayerNormFp32(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16 (by casting to float32 and back)."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        x = F.layer_norm(x.to(torch.float32), self.normalized_shape, self.weight, self.bias, self.eps)
        return x.to(orig_type)


class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm (with cast back to input dtype)."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        x = F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        return x.to(orig_type)


class QuickGELU(nn.Module):
    # NOTE This is slower than nn.GELU or nn.SiLU and uses more GPU memory
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)


class LayerScale(nn.Module):
    def __init__(self, dim, init_values=1e-5, inplace=False):
        super().__init__()
        self.inplace = inplace
        self.gamma = nn.Parameter(init_values * torch.ones(dim))

    def forward(self, x):
        return x.mul_(self.gamma) if self.inplace else x * self.gamma


class PatchDropout(nn.Module):
    """
    https://arxiv.org/abs/2212.00794
    """

    def __init__(self, prob, exclude_first_token=True):
        super().__init__()
        assert 0 <= prob < 1.
        self.prob = prob
        self.exclude_first_token = exclude_first_token  # exclude CLS token

    def forward(self, x):
        if not self.training or self.prob == 0.:
            return x

        if self.exclude_first_token:
            cls_tokens, x = x[:, :1], x[:, 1:]
        else:
            cls_tokens = torch.jit.annotate(torch.Tensor, x[:, :1])

        batch = x.size()[0]
        num_tokens = x.size()[1]

        batch_indices = torch.arange(batch)
        batch_indices = batch_indices[..., None]

        keep_prob = 1 - self.prob
        num_patches_keep = max(1, int(num_tokens * keep_prob))

        rand = torch.randn(batch, num_tokens)
        patch_indices_keep = rand.topk(num_patches_keep, dim=-1).indices

        x = x[batch_indices, patch_indices_keep]

        if self.exclude_first_token:
            x = torch.cat((cls_tokens, x), dim=1)

        return x


class Attention(nn.Module):
    def __init__(
            self,
            dim,
            num_heads=8,
            qkv_bias=True,
            scaled_cosine=False,
            scale_heads=False,
            logit_scale_max=math.log(1. / 0.01),
            attn_drop=0.,
            proj_drop=0.
    ):
        super().__init__()
        self.scaled_cosine = scaled_cosine
        self.scale_heads = scale_heads
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5
        self.logit_scale_max = logit_scale_max

        # keeping in_proj in this form (instead of nn.Linear) to match weight scheme of original
        self.in_proj_weight = nn.Parameter(torch.randn((dim * 3, dim)) * self.scale)
        if qkv_bias:
            self.in_proj_bias = nn.Parameter(torch.zeros(dim * 3))
        else:
            self.in_proj_bias = None

        if self.scaled_cosine:
            self.logit_scale = nn.Parameter(torch.log(10 * torch.ones((num_heads, 1, 1))))
        else:
            self.logit_scale = None
        self.attn_drop = nn.Dropout(attn_drop)
        if self.scale_heads:
            self.head_scale = nn.Parameter(torch.ones((num_heads, 1, 1)))
        else:
            self.head_scale = None
        self.out_proj = nn.Linear(dim, dim)
        self.out_drop = nn.Dropout(proj_drop)

    def forward(self, x, attn_mask: Optional[torch.Tensor] = None):
        L, N, C = x.shape
        q, k, v = F.linear(x, self.in_proj_weight, self.in_proj_bias).chunk(3, dim=-1)
        q = q.contiguous().view(L, N * self.num_heads, -1).transpose(0, 1)
        k = k.contiguous().view(L, N * self.num_heads, -1).transpose(0, 1)
        v = v.contiguous().view(L, N * self.num_heads, -1).transpose(0, 1)

        if self.logit_scale is not None:
            attn = torch.bmm(F.normalize(q, dim=-1), F.normalize(k, dim=-1).transpose(-1, -2))
            logit_scale = torch.clamp(self.logit_scale, max=self.logit_scale_max).exp()
            attn = attn.view(N, self.num_heads, L, L) * logit_scale
            attn = attn.view(-1, L, L)
        else:
            q = q * self.scale
            attn = torch.bmm(q, k.transpose(-1, -2))

        if attn_mask is not None:
            if attn_mask.dtype == torch.bool:
                new_attn_mask = torch.zeros_like(attn_mask, dtype=q.dtype)
                new_attn_mask.masked_fill_(attn_mask, float("-inf"))
                attn_mask = new_attn_mask
            attn += attn_mask

        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = torch.bmm(attn, v)
        if self.head_scale is not None:
            x = x.view(N, self.num_heads, L, C) * self.head_scale
            x = x.view(-1, L, C)
        x = x.transpose(0, 1).reshape(L, N, C)
        x = self.out_proj(x)
        x = self.out_drop(x)
        return x


class AttentionalPooler(nn.Module):
    def __init__(
            self,
            d_model: int,
            context_dim: int,
            n_head: int = 8,
            n_queries: int = 256,
            norm_layer: Callable = LayerNorm
    ):
        super().__init__()
        self.query = nn.Parameter(torch.randn(n_queries, d_model))
        self.attn = nn.MultiheadAttention(d_model, n_head, kdim=context_dim, vdim=context_dim)
        self.ln_q = norm_layer(d_model)
        self.ln_k = norm_layer(context_dim)

    def forward(self, x: torch.Tensor):
        x = self.ln_k(x).permute(1, 0, 2)  # NLD -> LND
        N = x.shape[1]
        q = self.ln_q(self.query)
        out = self.attn(self._repeat(q, N), x, x, need_weights=False)[0]
        return out.permute(1, 0, 2)  # LND -> NLD

    def _repeat(self, query, N: int):
        return query.unsqueeze(1).repeat(1, N, 1)


class ResidualAttentionBlock(nn.Module):
    def __init__(
            self,
            d_model: int,
            n_head: int,
            mlp_ratio: float = 4.0,
            ls_init_value: float = None,
            act_layer: Callable = nn.GELU,
            norm_layer: Callable = LayerNorm,
            is_cross_attention: bool = False,
    ):
        super().__init__()

        self.ln_1 = norm_layer(d_model)
        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ls_1 = LayerScale(d_model, ls_init_value) if ls_init_value is not None else nn.Identity()
        if is_cross_attention:
            self.ln_1_kv = norm_layer(d_model)

        self.ln_2 = norm_layer(d_model)
        mlp_width = int(d_model * mlp_ratio)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, mlp_width)),
            ("gelu", act_layer()),
            ("c_proj", nn.Linear(mlp_width, d_model))
        ]))
        self.ls_2 = LayerScale(d_model, ls_init_value) if ls_init_value is not None else nn.Identity()

    def attention(
            self,
            q_x: torch.Tensor,
            k_x: Optional[torch.Tensor] = None,
            v_x: Optional[torch.Tensor] = None,
            attn_mask: Optional[torch.Tensor] = None,
    ):
        k_x = k_x if k_x is not None else q_x
        v_x = v_x if v_x is not None else q_x

        # attn_mask = attn_mask.to(q_x.dtype) if attn_mask is not None else None
        return self.attn(
            q_x, k_x, v_x, need_weights=False, attn_mask=attn_mask
        )[0]

    def forward(
            self,
            q_x: torch.Tensor,
            k_x: Optional[torch.Tensor] = None,
            v_x: Optional[torch.Tensor] = None,
            attn_mask: Optional[torch.Tensor] = None,
    ):
        k_x = self.ln_1_kv(k_x) if hasattr(self, "ln_1_kv") and k_x is not None else None
        v_x = self.ln_1_kv(v_x) if hasattr(self, "ln_1_kv") and v_x is not None else None

        x = q_x + self.ls_1(self.attention(q_x=self.ln_1(q_x), k_x=k_x, v_x=v_x, attn_mask=attn_mask))
        x = x + self.ls_2(self.mlp(self.ln_2(x)))
        return x


class ResidualAttentionBlockV2(ResidualAttentionBlock):
    def proj_without_attn(self, value):
        attn_module = self.attn
        value = F.linear(value, attn_module.in_proj_weight,
                         bias=attn_module.in_proj_bias)[..., -attn_module.embed_dim:]
        value = F.linear(value, attn_module.out_proj.weight,
                         bias=attn_module.out_proj.bias)

        return value

    def forward_without_attn(self, q_x):
        x = q_x + self.ls_1(self.proj_without_attn(value=self.ln_1(q_x)))    # use the maskclip-zhou style
        x = x + self.ls_2(self.mlp(self.ln_2(x)))
        return x
    
    def csa_attn(self, x, mode):
        x = self.ln_1(x)
        attn_layer = self.attn
        num_heads = attn_layer.num_heads
        _, bsz, embed_dim = x.size()
        head_dim = embed_dim // num_heads
        scale = head_dim ** -0.5
        q, k, v = F.linear(x, attn_layer.in_proj_weight, attn_layer.in_proj_bias).chunk(3, dim=-1)
        q = q.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
        k = k.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
        v = v.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
        q_attn = torch.bmm(q, q.transpose(1, 2)) * scale
        k_attn = torch.bmm(k, k.transpose(1, 2)) * scale
        attn_weights = F.softmax(q_attn, dim=-1) + F.softmax(k_attn, dim=-1)
        attn_output = torch.bmm(attn_weights, v)
        attn_output = attn_output.transpose(0, 1).contiguous().view(-1, bsz, embed_dim)
        attn_output = attn_layer.out_proj(attn_output)
        attn_output=self.ls_1(attn_output)
        if "distill" in mode:
            return attn_output, (q[:, 1:], k[:, 1:])
        else:
            return attn_output
    
    def ss_attn(self, x, mode):
        x = self.ln_1(x)
        attn_layer = self.attn
        num_heads = attn_layer.num_heads
        _, bsz, embed_dim = x.size()
        head_dim = embed_dim // num_heads
        scale = head_dim ** -0.5
        q, k, v = F.linear(x, attn_layer.in_proj_weight, attn_layer.in_proj_bias).chunk(3, dim=-1)
        q = q.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
        k = k.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
        v = v.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
        if mode=="qq" or mode=="qq_vfm_distill":
            q_attn = torch.bmm(q, q.transpose(1, 2)) *  scale
            attn_weights = F.softmax(q_attn, dim=-1) 
        elif mode=="kk" or mode=="kk_vfm_distill":
            k_attn = torch.bmm(k, k.transpose(1, 2)) *  scale
            attn_weights = F.softmax(k_attn, dim=-1) 
        else:
            raise NotImplementedError(f"The mode '{mode}' is not implemented.")
        attn_output = torch.bmm(attn_weights, v)
        attn_output = attn_output.transpose(0, 1).contiguous().view(-1, bsz, embed_dim)
        attn_output = attn_layer.out_proj(attn_output)
        attn_output=self.ls_1(attn_output)
        if mode=='qq_vfm_distill':
            return attn_output, q[:, 1:]
        elif mode == 'kk_vfm_distill':
            return attn_output, k[:, 1:]
        else:
            return attn_output

class Transformer(nn.Module):
    def __init__(
            self,
            width: int,
            layers: int,
            heads: int,
            mlp_ratio: float = 4.0,
            ls_init_value: float = None,
            act_layer: Callable = nn.GELU,
            norm_layer: Callable = LayerNorm,
    ):
        super().__init__()
        self.width = width
        self.layers = layers
        self.grad_checkpointing = False

        self.resblocks = nn.ModuleList([
            ResidualAttentionBlockV2(
                width, heads, mlp_ratio, ls_init_value=ls_init_value, act_layer=act_layer, norm_layer=norm_layer)
            for _ in range(layers)
        ])

    def get_cast_dtype(self) -> torch.dtype:
        return self.resblocks[0].mlp.c_fc.weight.dtype

    def forward(self, x: torch.Tensor, attn_mask: Optional[torch.Tensor] = None):
        for r in self.resblocks:
            if self.grad_checkpointing and not torch.jit.is_scripting():
                # TODO: handle kwargs https://github.com/pytorch/pytorch/issues/79887#issuecomment-1161758372
                x = checkpoint(r, x, None, None, attn_mask)
            else:
                x = r(x, attn_mask=attn_mask)
        return x

    def extract_feature_map(self, x, return_forward=False,mode='maskclip'):
        for i in range(self.layers - 1):
            x = self.resblocks[i](x)
        if mode=='maskclip':
            x = self.resblocks[-1].forward_without_attn(x)
        elif mode in ['csa','csa_vfm_distill']:
            x = self.resblocks[-1].csa_attn(x,mode)
        elif mode in ['qq','kk','qq_vfm_distill','kk_vfm_distill']:
            x = self.resblocks[-1].ss_attn(x, mode)
        else:
            raise NotImplementedError(f"The mode '{mode}' is not implemented.")
        return x

    def forward_image_dense(self, x, attn_mask):
        for i in range(self.layers - 1):
            x = self.resblocks[i](x, attn_mask=attn_mask)
        dense = self.resblocks[-1].forward_without_attn(x)
        image = self.resblocks[-1](x, attn_mask=attn_mask)

        return image, dense


class VisionTransformer(nn.Module):
    output_tokens: torch.jit.Final[bool]

    def __init__(
            self,
            image_size: int,
            patch_size: int,
            width: int,
            layers: int,
            heads: int,
            mlp_ratio: float,
            ls_init_value: float = None,
            global_average_pool: bool = False,
            attentional_pool: bool = False,
            n_queries: int = 256,
            attn_pooler_heads: int = 8,
            output_dim: int = 512,
            patch_dropout: float = 0.,
            input_patchnorm: bool = False,
            act_layer: Callable = nn.GELU,
            norm_layer: Callable = LayerNorm,
            output_tokens: bool = False
    ):
        super().__init__()
        self.output_tokens = output_tokens
        image_height, image_width = self.image_size = to_2tuple(image_size)
        patch_height, patch_width = self.patch_size = to_2tuple(patch_size)
        self.grid_size = (image_height // patch_height, image_width // patch_width)
        self.output_dim = output_dim

        # whether to layernorm each patch, as done in dual patchnorm paper - https://arxiv.org/abs/2302.01327v1
        self.input_patchnorm = input_patchnorm
        assert not input_patchnorm
        if input_patchnorm:
            patch_input_dim = patch_height * patch_width * 3
            self.patchnorm_pre_ln = LayerNorm(patch_input_dim)
            self.conv1 = nn.Linear(patch_input_dim, width)
        else:
            self.patchnorm_pre_ln = nn.Identity()
            self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)

        # class embeddings and positional embeddings
        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(scale * torch.randn(self.grid_size[0] * self.grid_size[1] + 1, width))

        # setting a patch_dropout of 0. would mean it is disabled and this function would be the identity fn
        self.patch_dropout = PatchDropout(patch_dropout) if patch_dropout > 0. else nn.Identity()

        self.ln_pre = norm_layer(width)
        self.transformer = Transformer(
            width,
            layers,
            heads,
            mlp_ratio,
            ls_init_value=ls_init_value,
            act_layer=act_layer,
            norm_layer=norm_layer,
        )
        self.num_heads = heads

        self.global_average_pool = global_average_pool
        if attentional_pool:
            self.attn_pool = AttentionalPooler(output_dim, width, n_head=attn_pooler_heads, n_queries=n_queries)
            self.ln_post = norm_layer(output_dim)
            self.proj = nn.Parameter(scale * torch.randn(output_dim, output_dim))
        else:
            self.attn_pool = None
            self.ln_post = norm_layer(width)
            self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

        self.init_parameters()

    def lock(self, unlocked_groups=0, freeze_bn_stats=False):
        for param in self.parameters():
            param.requires_grad = False

        if unlocked_groups != 0:
            groups = [
                [
                    self.conv1,
                    self.class_embedding,
                    self.ln_pre,
                ],
                self.positional_embedding,
                *self.transformer.resblocks[:-1],
                [
                    self.transformer.resblocks[-1],
                    # self.ln_post,     # fix layer norm
                ],
                # self.proj,        # fix output layers
            ]

            def _unlock(x):
                if isinstance(x, Sequence):
                    for g in x:
                        _unlock(g)
                else:
                    if isinstance(x, torch.nn.Parameter):
                        x.requires_grad = True
                    else:
                        for p in x.parameters():
                            p.requires_grad = True

            _unlock(groups[-unlocked_groups:])

    def attention_lock(self, **kwargs):
        for name, params in self.named_parameters():
            params.requires_grad = True if "attn" in name or "position" in name else False

    def init_parameters(self):
        # FIXME OpenAI CLIP did not define an init for the VisualTransformer
        # TODO experiment if default PyTorch init, below, or alternate init is best.
        pass

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.transformer.grad_checkpointing = enable

    def _global_pool(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        if self.global_average_pool: # false
            return x.mean(dim=1), x
        else:
            return x[:, 0], x[:, 1:]

    def forward(self, x: torch.Tensor):

        x = self.conv1(x)  # shape = [*, width, grid, grid]
        bs, _, h, w = x.shape
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]

        # class embeddings and positional embeddings
        x = torch.cat(
            [self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
             x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        # TODO: Allow interpolating the positional embeddings

        if (h, w) == self.grid_size:
            pe = self.positional_embedding.to(x.dtype)
        else:
            pe = self.rescale_positional_embedding(out_size=(h, w), dtype=x.dtype)

        x = x + pe

        # a patch_dropout of 0. would mean it is disabled and this function would do nothing but return what was passed in
        x = self.patch_dropout(x)
        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD

        if self.attn_pool is not None:
            x = self.attn_pool(x)
            x = self.ln_post(x)
            pooled, tokens = self._global_pool(x)
        else:
            pooled, tokens = self._global_pool(x)
            pooled = self.ln_post(pooled)

        if self.proj is not None:
            pooled = pooled @ self.proj

        if self.output_tokens:
            return pooled, tokens
        
        return pooled

    def extract_roi_features(self, x, normed_boxes, mode="qq_vfm_distill"):
        if mode=="qq_vfm_distill" or mode=="kk_vfm_distill" or mode=="csa_vfm_distill":
            x, extra_feats = self.encode_dense(x, keep_shape=True, mode=mode)
            return roi_align(x, self._denormalize_boxes(normed_boxes, x), (1, 1),1.0, -1, True)[..., 0, 0], extra_feats
        else:
            x = self.encode_dense(x, keep_shape=True,mode=mode)
            return roi_align(x, self._denormalize_boxes(normed_boxes, x), (1, 1),1.0, -1, True)[..., 0, 0]

    def mask_pool(self, x, masks, mode="qq_vfm_distill"):
        feature_map = self.encode_dense(x, keep_shape=False,mode=mode)
        num_masks_per_image = [len(masks_per_image) for masks_per_image in masks]
        masks = torch.cat(masks).float().flatten(-2, -1)    # bs, h*w
        feature_map = torch.repeat_interleave(feature_map, torch.tensor(num_masks_per_image, device=feature_map.device), dim=0)
        features = (feature_map * masks.unsqueeze(-1)).sum(1) / (masks.sum(1, keepdim=True) + 1e-12)
        return features

    def encode_dense(self, x, keep_shape=False, mode='maskclip'):
        x = self.conv1(x)  # shape = [*, width, grid, grid]
        bs, _, h, w = x.shape
        # assert h == w  # TODO: support input of any shape, need to change the normed boxes to real boxes
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
        x = torch.cat(
            [self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
             x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        if (h, w) == self.grid_size:
            pe = self.positional_embedding.to(x.dtype)
        else:
            pe = self.rescale_positional_embedding(out_size=(h, w), dtype=x.dtype)

        x = x + pe

        # a patch_dropout of 0. would mean it is disabled and this function would do nothing but return what was passed in
        x = self.patch_dropout(x)
        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)  # NLD -> LND
        if 'distill' in mode:
            x, extra_feats = self.transformer.extract_feature_map(x, mode=mode)
        else:
            x = self.transformer.extract_feature_map(x, mode=mode)
        x = x.permute(1, 0, 2)  # LND -> NLD
        if self.attn_pool is not None:
            x = self.attn_pool(x)
            x = self.ln_post(x)
            _, tokens = self._global_pool(x)
        else:
            _, tokens = self._global_pool(x)
            tokens = self.ln_post(tokens)
        if self.proj is not None:
            tokens = tokens @ self.proj
        feature_map = tokens.view(bs, h * w, -1)   # .permute(0, 3, 1, 2)
        feature_map = F.normalize(feature_map, dim=-1)  # FIXME code from clipself, will slightly affect performance
        if keep_shape:
            feature_map = feature_map.view(bs, h, w, -1).permute(0, 3, 1, 2)
        if 'distill' in mode:
            return feature_map, extra_feats
        else:
            return feature_map

    @staticmethod
    def _denormalize_boxes(normed_boxes, x):
        h, w = x.shape[-2:]
        denormed_boxes = []
        for boxes in normed_boxes:
            new_boxes = boxes.clone()     # FIXME: do not change the value in normed_boxes!
            new_boxes[:, [0, 2]] *= w
            new_boxes[:, [1, 3]] *= h
            denormed_boxes.append(new_boxes)
        return denormed_boxes
    
    def rescale_positional_embedding(self, out_size, dtype):
        h, w = out_size
        rescaled_positional_embedding = \
            self.positional_embedding.new_zeros(1 + h*w, self.positional_embedding.shape[1])
        rescaled_positional_embedding[0] = self.positional_embedding[0]
        pe_2d = self.positional_embedding[1:].T.contiguous().view(
            1, -1, *self.grid_size)
        pe_2d = F.interpolate(pe_2d, out_size, mode='bicubic', align_corners=False).view(-1, h*w)
        rescaled_positional_embedding[1:] = pe_2d.T.contiguous()

        return rescaled_positional_embedding.to(dtype=dtype)





class TextTransformer(nn.Module):
    output_tokens: torch.jit.Final[bool]

    def __init__(
            self,
            context_length: int = 77,
            vocab_size: int = 49408,
            width: int = 512,
            heads: int = 8,
            layers: int = 12,
            ls_init_value: float = None,
            output_dim: int = 512,
            act_layer: Callable = nn.GELU,
            norm_layer: Callable = LayerNorm,
            embed_cls: bool = False,
            pad_id: int = 0,
            output_tokens: bool = False,
    ):
        super().__init__()
        self.output_tokens = output_tokens
        self.num_pos = self.context_length = context_length
        self.vocab_size = vocab_size
        self.width = width
        self.output_dim = output_dim
        self.heads = heads
        self.pad_id = pad_id

        self.text_projection = nn.Parameter(torch.empty(width, output_dim))

        if embed_cls:
            self.cls_emb = nn.Parameter(torch.empty(width))
            self.num_pos += 1
        else:
            self.cls_emb = None

        self.token_embedding = nn.Embedding(vocab_size, width)
        self.positional_embedding = nn.Parameter(torch.empty(self.num_pos, width))
        self.transformer = Transformer(
            width=width,
            layers=layers,
            heads=heads,
            ls_init_value=ls_init_value,
            act_layer=act_layer,
            norm_layer=norm_layer,
        )
        self.ln_final = norm_layer(width)

        self.register_buffer('attn_mask', self.build_attention_mask(), persistent=False)

        self.init_parameters()

    def init_parameters(self):
        nn.init.normal_(self.token_embedding.weight, std=0.02)
        nn.init.normal_(self.positional_embedding, std=0.01)
        if self.cls_emb is not None:
            nn.init.normal_(self.cls_emb, std=0.01)

        proj_std = (self.transformer.width ** -0.5) * ((2 * self.transformer.layers) ** -0.5)
        attn_std = self.transformer.width ** -0.5
        fc_std = (2 * self.transformer.width) ** -0.5
        for block in self.transformer.resblocks:
            nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
            nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
            nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
            nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)

        if self.text_projection is not None:
            nn.init.normal_(self.text_projection, std=self.transformer.width ** -0.5)

    def lock(self, unlocked_layers: int = 0, freeze_layer_norm: bool = True):
        assert unlocked_layers == 0 and freeze_layer_norm
        print(f'Freeze the text encoder', flush=True)
        for p in self.parameters():
            p.requires_grad = False

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.transformer.grad_checkpointing = enable

    def build_attention_mask(self):
        # lazily create causal attention mask, with full attention between the tokens
        # pytorch uses additive attention mask; fill with -inf
        mask = torch.empty(self.num_pos, self.num_pos)
        mask.fill_(float("-inf"))
        mask.triu_(1)  # zero out the lower diagonal
        return mask

    def build_cls_mask(self, text, cast_dtype: torch.dtype):
        cls_mask = (text != self.pad_id).unsqueeze(1)
        cls_mask = F.pad(cls_mask, (1, 0, cls_mask.shape[2], 0), value=1.0)
        additive_mask = torch.empty(cls_mask.shape, dtype=cast_dtype, device=cls_mask.device)
        additive_mask.fill_(0)
        additive_mask.masked_fill_(~cls_mask, float("-inf"))
        additive_mask = torch.repeat_interleave(additive_mask, self.heads, 0)
        return additive_mask

    def _repeat(self, t, N: int):
        return t.reshape(1, 1, -1).repeat(N, 1, 1)

    def forward(self, text):
        cast_dtype = self.transformer.get_cast_dtype()
        seq_len = text.shape[1]

        x = self.token_embedding(text).to(cast_dtype)  # [batch_size, n_ctx, d_model]
        attn_mask = self.attn_mask
        if self.cls_emb is not None:
            seq_len += 1
            x = torch.cat([x, self._repeat(self.cls_emb, x.shape[0])], dim=1)
            cls_mask = self.build_cls_mask(text, cast_dtype)
            attn_mask = attn_mask[None, :seq_len, :seq_len] + cls_mask[:, :seq_len, :seq_len]

        x = x + self.positional_embedding[:seq_len].to(cast_dtype)
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x, attn_mask=attn_mask)
        x = x.permute(1, 0, 2)  # LND -> NLD

        # x.shape = [batch_size, n_ctx, transformer.width]
        # take features from the eot embedding (eot_token is the highest number in each sequence)
        if self.cls_emb is not None:
            pooled, tokens = x[:, -1], x[:, :-1]
            pooled = self.ln_final(pooled)
        else:
            x = self.ln_final(x)
            pooled, tokens = x[torch.arange(x.shape[0]), text.argmax(dim=-1)], x

        if self.text_projection is not None:
            pooled = pooled @ self.text_projection

        if self.output_tokens:
            return pooled, tokens

        return pooled


class MultimodalTransformer(Transformer):
    def __init__(
            self,
            width: int,
            layers: int,
            heads: int,
            context_length: int = 77,
            mlp_ratio: float = 4.0,
            ls_init_value: float = None,
            act_layer: Callable = nn.GELU,
            norm_layer: Callable = LayerNorm,
            output_dim: int = 512,
    ):

        super().__init__(
            width=width,
            layers=layers,
            heads=heads,
            mlp_ratio=mlp_ratio,
            ls_init_value=ls_init_value,
            act_layer=act_layer,
            norm_layer=norm_layer,
        )
        self.context_length = context_length
        self.cross_attn = nn.ModuleList([
            ResidualAttentionBlock(
                width,
                heads,
                mlp_ratio,
                ls_init_value=ls_init_value,
                act_layer=act_layer,
                norm_layer=norm_layer,
                is_cross_attention=True,
            )
            for _ in range(layers)
        ])

        self.register_buffer('attn_mask', self.build_attention_mask(), persistent=False)

        self.ln_final = norm_layer(width)
        self.text_projection = nn.Parameter(torch.empty(width, output_dim))

    def init_parameters(self):
        proj_std = (self.transformer.width ** -0.5) * ((2 * self.transformer.layers) ** -0.5)
        attn_std = self.transformer.width ** -0.5
        fc_std = (2 * self.transformer.width) ** -0.5
        for block in self.transformer.resblocks:
            nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
            nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
            nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
            nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)
        for block in self.transformer.cross_attn:
            nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
            nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
            nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
            nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)

        if self.text_projection is not None:
            nn.init.normal_(self.text_projection, std=self.transformer.width ** -0.5)

    def build_attention_mask(self):
        # lazily create causal attention mask, with full attention between the tokens
        # pytorch uses additive attention mask; fill with -inf
        mask = torch.empty(self.context_length, self.context_length)
        mask.fill_(float("-inf"))
        mask.triu_(1)  # zero out the lower diagonal
        return mask

    def forward(self, image_embs, text_embs):
        text_embs = text_embs.permute(1, 0, 2)  # NLD -> LNDsq
        image_embs = image_embs.permute(1, 0, 2)  # NLD -> LND
        seq_len = text_embs.shape[0]

        for resblock, cross_attn in zip(self.resblocks, self.cross_attn):
            if self.grad_checkpointing and not torch.jit.is_scripting():
                # TODO: handle kwargs https://github.com/pytorch/pytorch/issues/79887#issuecomment-1161758372
                text_embs = checkpoint(resblock, text_embs, None, None, self.attn_mask[:seq_len, :seq_len])
                text_embs = checkpoint(cross_attn, text_embs, image_embs, image_embs, None)
            else:
                text_embs = resblock(text_embs, attn_mask=self.attn_mask[:seq_len, :seq_len])
                text_embs = cross_attn(text_embs, k_x=image_embs, v_x=image_embs)

        x = text_embs.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_final(x)

        if self.text_projection is not None:
            x = x @ self.text_projection

        return x

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.grad_checkpointing = enable
