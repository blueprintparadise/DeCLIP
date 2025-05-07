data_root=/mnt/SSD8T/home/wjj/dataset/standard_coco
pretrain_ckpt=/mnt/SSD8T/home/wjj/code/my_CLIPSelf/checkpoints/EVA02_CLIP_B_psz16_s8B.pt
exp_name=evab_dinov2B_qq_560
vfm_type=dinov2-B # {sam-B, sam-L, dinov2-B, dinov2-L, dino-B-8, dino-B-16}

# always keep total batchsize=16 , otherwise, Linear scaling the learning rate
CUDA_VISIBLE_DEVICES=0,1,2,3 torchrun --nproc_per_node 4 --master_port 29500 -m training.main --batch-size=4 --lr=1e-5 --wd=0.1 --epochs=6 --workers=4 \
--model EVA02-CLIP-B-16 --pretrained eva --warmup 1000 --zeroshot-frequency 1 --dataset-type grid_distill \
--test-type coco_panoptic --train-data ${data_root}/annotations/instances_train2017.json \
--val-data ${data_root}/annotations/panoptic_val2017.json \
--embed-path metadata/coco_panoptic_clip_hand_craft_EVACLIP_ViTB16.npy --train-image-root ${data_root}/train2017 \
--val-image-root ${data_root}/val2017 --cache-dir ${pretrain_ckpt} --log-every-n-steps 100 \
--lock-image --save-frequency 1 --lock-image-unlocked-groups 12 \
--name ${exp_name} --downsample-factor 16 --det-image-size 560 --val-segm-root ${data_root}/annotations/panoptic_val2017 \
--alpha 0.7 --mode qq_vfm_distill  --use_vfm ${vfm_type} --loss_context_weight 0.1 --loss_content_weight 1.0 --skip-first-eval
