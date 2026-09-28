"""YOLOX-Tiny，1 类车牌检测（plate），输入 640x640（NRD-3 §6.6 / P5）。

训练链与导出沿用 NRD-2 §S5（YOLOX 0.3.0，PYTHONPATH 导入，opset 11，
decode_in_inference=False，输入名 images，BGR 0-255，letterbox pad 114）。

数据由 training/build_det_dataset.py 生成：
  train = CCPD2019 ccpd_base 随机 5 万张（seed 42）+ CCPD2020 ccpd_green train 全部
  val   = CCPD2019 ccpd_base 与 train 不相交的 2500 张（5%）+ ccpd2020 val 全部
（§7：检测 = CCPD2019 base 子集随机 5 万 + CCPD2020 全部，验证 5%；
 本期不做海外制式（§0.1），不含合成 DE 整图。）
"""
import os

from yolox.exp import Exp as MyExp


class Exp(MyExp):
    def __init__(self):
        super().__init__()

        # --- 模型：YOLOX-Tiny，1 类 plate ---
        self.depth = 0.33
        self.width = 0.375
        self.num_classes = 1
        self.exp_name = "yolox_tiny_plate"

        # --- 数据（build_det_dataset.py 产物；COCO 布局 train2017/val2017） ---
        self.data_dir = os.environ.get(
            "PLATE_DET_DATA_DIR",
            os.path.expanduser("~/edge-parking-vision/data/coco/plate_det"))
        self.train_ann = "instances_train2017.json"
        self.val_ann = "instances_val2017.json"
        self.test_ann = "instances_val2017.json"
        self.train_name = "train2017"
        self.val_name = "val2017"
        self.test_name = "val2017"
        self.data_num_workers = 8
        self.output_dir = os.environ.get(
            "PLATE_DET_OUTPUT_DIR",
            os.path.expanduser("~/edge-parking-vision/training/outputs"))

        # --- 输入尺寸：训练/测试固定 640²；416 由同一权重改 test_size 导出 ---
        self.input_size = (640, 640)
        self.test_size = (640, 640)
        # multiscale_range=0 让 random_resize 恒返回 640（勿显式设 random_size，
        # 0.3.0 用 hasattr 判断，设成 None 会崩）。
        self.multiscale_range = 0

        # --- 增强：CCPD 为彩色实拍，保留 HSV；车牌小目标，mosaic 尺度不放大太多 ---
        self.mosaic_prob = 1.0
        self.mixup_prob = 1.0
        self.hsv_prob = 1.0
        self.flip_prob = 0.5
        self.degrees = 10.0
        self.translate = 0.1
        self.mosaic_scale = (0.5, 1.5)
        self.mixup_scale = (0.5, 1.5)
        self.shear = 2.0
        self.enable_mixup = True

        # --- 训练调度 ---
        self.max_epoch = int(os.environ.get("PLATE_DET_MAX_EPOCH", 40))
        self.no_aug_epochs = 10          # 末 10 epoch 关闭 Mosaic/MixUp
        self.warmup_epochs = 3
        self.basic_lr_per_img = 0.01 / 64.0
        self.scheduler = "yoloxwarmcos"
        self.min_lr_ratio = 0.05
        self.weight_decay = 5e-4
        self.momentum = 0.9
        self.ema = True

        self.print_interval = 50
        self.eval_interval = 5           # 每 5 epoch 评一次，保留最佳 mAP checkpoint
        self.save_history_ckpt = False

        # --- 评测 ---
        self.test_conf = 0.01
        self.nmsthre = 0.65
