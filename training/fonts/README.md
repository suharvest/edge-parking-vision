# training/fonts —— 合成车牌字体

NRD-3 §8 P3 / §7「合成车牌生成器」用字体。**字体文件不入库**（下方下载到本目录）。

## Noto Sans CJK SC Bold（本期唯一需要的字体）

| 项 | 值 |
|---|---|
| 用途 | `training/plate_synth.py` 绘制中国大陆车牌（省份简称与字母数字；Alte DIN 1451 随德国制式暂缓，§0.1） |
| 许可 | **SIL Open Font License 1.1** |
| 许可原文（仓库） | https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE |
| 商用 | 允许（字体不得单独出售） |

### 许可核实结论（2026-09-28，NRD-3 §7「需核实」项）

从上述仓库 raw 地址（`https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE`）
取得 LICENSE 全文（92 行）：开头即「This Font Software is licensed under the SIL Open
Font License, Version 1.1.」，其后为 OFL-1.1 标准全文与版权行「Copyright 2014-2021
Adobe (http://www.adobe.com/)」。**结论：SIL OFL-1.1 属实，可用且允许商用**；已按 §7
要求将 LICENSE 原文存档于本文件末尾，并登记进仓库根 `LICENSES.md`。

### 下载

```bash
curl -L -o training/fonts/NotoSansCJKsc-Bold.otf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf
```

（约 16 MB；也可用同仓库 Variable 版 `Sans/Variable/OTF/NotoSansCJKsc-VF.otf`。）

## 暂缓（本期不做，§0.1）

- Alte DIN 1451 Mittelschrift（德国牌）：许可来源矛盾（OFL-1.1 或 CC BY 3.0 DE），
  待德国制式恢复时以字体压缩包内自带许可文件为准再核实。

---

## 附录：Noto Sans CJK LICENSE 原文存档

来源：notofonts/noto-cjk `main` 分支 `Sans/LICENSE`（2026-09-28 取得）。

```text
This Font Software is licensed under the SIL Open Font License,
Version 1.1.

This license is copied below, and is also available with a FAQ at:
http://scripts.sil.org/OFL

-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font
creation efforts of academic and linguistic communities, and to
provide a free and open framework in which fonts may be shared and
improved in partnership with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply to
any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software
components as distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to,
deleting, or substituting -- in part or in whole -- any of the
components of the Original Version, by changing formats or by porting
the Font Software to a new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed,
modify, redistribute, and sell modified and unmodified copies of the
Font Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components, in
Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the
corresponding Copyright Holder. This restriction only applies to the
primary font name as presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created using
the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```
