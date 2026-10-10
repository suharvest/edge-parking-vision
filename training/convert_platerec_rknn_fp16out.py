"""plate_rec w8a8 with final 'output' Add as float16 (hybrid quantization), identical config to
acceptance-20261008/cat-remote/round2/convert/convert_rec_w8a8_fp16out.py; ONNX and calibration list
are arguments. usage: python convert_rec_w8a8_fp16out.py <target> <onnx> <dataset.txt> <out_dir>"""
import os, sys, threading
sys.path.insert(0, "/home/harvest/sv-rknn-2320/.venv/lib/python3.10/site-packages")
from rknn.api import RKNN
TARGET, ONNX, DS, OUT = sys.argv[1], os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]), os.path.abspath(sys.argv[4])
STEM = os.path.basename(ONNX).removesuffix(".onnx"); FP16 = ["output"]
W = f"{OUT}/work_rec_{TARGET}"; os.makedirs(W, exist_ok=True); os.chdir(W)
err = []
def run():
    try:
        rk = RKNN(verbose=False)
        assert rk.config(mean_values=[[0, 0, 0]], std_values=[[255.0] * 3], target_platform=TARGET,
                         quantized_dtype="w8a8", optimization_level=3) == 0
        assert rk.load_onnx(model=ONNX) == 0
        assert rk.hybrid_quantization_step1(dataset=DS, proposal=False) == 0
        rk.release()
        cfg = open(f"{STEM}.quantization.cfg").read()
        cfg = cfg.replace("custom_quantize_layers: {}", "custom_quantize_layers:\n" + "".join(f"    '{t}': float16\n" for t in FP16), 1)
        open(f"{STEM}.quantization.cfg", "w").write(cfg)
        rk = RKNN(verbose=False)
        assert rk.hybrid_quantization_step2(model_input=f"{STEM}.model", data_input=f"{STEM}.data",
                                            model_quantization_cfg=f"{STEM}.quantization.cfg") == 0
        assert rk.export_rknn(f"{OUT}/plate_rec_cn_96x32.{TARGET}.int8.rknn") == 0
        rk.release()
    except Exception:
        import traceback; traceback.print_exc(); err.append(1)
threading.stack_size(256 * 1024 * 1024)
t = threading.Thread(target=run); t.start(); t.join()
sys.exit(1 if err else 0)
