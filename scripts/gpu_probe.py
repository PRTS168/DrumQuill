# -*- coding: utf-8 -*-
"""Probe whether this interpreter can run Demucs on CUDA.

Prints a single JSON line; used by app.py (/api/gpu) and run_pipeline.ps1.
Exit code is always 0 so callers can parse stdout regardless of the verdict.

除了"能不能用"，还顺带给出该卡的显存对应的分离段长（见 gpu_detect.best_segment），
管线据此挑 --segment，避免小显存卡一上来就 OOM。
"""
import json
import os
import sys

# gpu_detect.py 在 app\ 下（本文件在 scripts\），两个目录都加进来
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    info = {"available": False, "reason": "", "name": "", "vram_mb": 0,
            "torch": "", "cuda": "", "cc": "", "arch_ok": False,
            "segment": 7, "ladder": [7, 5, 4]}
    try:
        import torch  # noqa: F401
        info["torch"] = torch.__version__
        info["cuda"] = torch.version.cuda or ""
        if not torch.cuda.is_available():
            info["reason"] = "torch.cuda.is_available() = False"
        else:
            prop = torch.cuda.get_device_properties(0)
            cc = "%d.%d" % (prop.major, prop.minor)
            info["available"] = True
            info["name"] = str(prop.name)
            info["vram_mb"] = int(prop.total_memory / 1048576)
            info["cc"] = cc
            try:
                archs = list(torch.cuda.get_arch_list())
                major, minor = prop.major, prop.minor
                ok = False
                for a in archs:
                    a = a.strip().lower()
                    if a.startswith("sm_") or a.startswith("compute_"):
                        num = a.split("_", 1)[1]
                        try:
                            if "." in num:
                                amaj, amin = [int(x) for x in num.split(".")[:2]]
                            elif len(num) <= 2:          # "50" -> 5.0, "86" -> 8.6
                                amaj, amin = int(num[0]), int(num[1])
                            else:                        # "120" -> 12.0
                                amaj, amin = int(num[:-1]), int(num[-1])
                        except Exception:
                            continue
                        # 同一主版本内：低版本的二进制可以在高版本卡上跑（Maxwell 5.0 -> 5.2）
                        if amaj == major and amin <= minor:
                            ok = True
                            break
                        # PTX（compute_*）只要主版本不高于本卡，可由驱动 JIT
                        if a.startswith("compute_") and amaj <= major:
                            ok = True
                            break
                info["arch_ok"] = ok
                info["archs"] = archs
            except Exception:
                # 解析异常时保守判不兼容（旧写法 fail-open=True：torch 版本变化误报兼容，
                # 运行时 Demucs 才崩）。管线据此走 CPU 段长，不赌未知架构。
                info["arch_ok"] = False
                info["arch_reason"] = "无法确认 CUDA 架构兼容性"
    except Exception as e:
        info["reason"] = "%s: %s" % (type(e).__name__, str(e)[:200])

    # 段长建议（即使不可用也给个安全值，管线会在 CPU 分支忽略它）
    try:
        import gpu_detect
        info["segment"] = gpu_detect.best_segment(info.get("vram_mb"))
        info["ladder"] = gpu_detect.segment_ladder(info.get("vram_mb"), info["segment"])
    except Exception:
        pass

    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
