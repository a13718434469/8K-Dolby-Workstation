# Independent Dolby Vision analysis

`dolby_analysis.py` implements an independent CPU measurement method, not Dolby's
proprietary CM analysis or certification. The installed dovi_tool supplies the
actual Profile 8.4 polynomial/MMR/DM coefficients. A generic HLG OOTF is never
substituted for this composer.

References:

- [SMPTE ST 2094-10:2021, sections 6.1.2–6.1.5](https://pub.smpte.org/latest/st2094-10/st2094-10-2021.pdf).
- [ETSI GS CCM 001, composer processing](https://www.etsi.org/deliver/etsi_gs/CCM/001_099/001/01.01.01_60/gs_ccm001v010101p.pdf).
- [ETSI TS 103 572 v1.2.1, 4095-grid quantization](https://www.etsi.org/deliver/etsi_TS/103500_103599/103572/01.02.01_60/ts_103572v010201p.pdf).
- [dovi_tool 2.3.4 Profile84 and Level1 limits](https://github.com/quietvoid/dovi_tool/tree/2.3.4/dolby_vision/src/rpu).
- [Published Dolby Vision HPE LMS→BT.2020 mathematical matrix](https://github.com/haasn/libplacebo/blob/master/src/shaders/colorspace.c).

Chroma MMR runs on the native 4:2:0 grid before bilinear upsampling. Inputs and
pivots use `2^BL_bit_depth`. The RGB→LMS→BT.2020 transform is retained. Spatial
reduction explicitly averages PQ RGB components in 2×2 blocks; the public text
does not explicitly fix this averaging domain. Quantization rounds halves upwards
on the 4095 grid. Raw measurements and CM4-clamped values are both retained.
No proprietary temporal smoothing, active-area detection or artistic trims are
claimed. Bilinear reconstruction is a declared interpolation choice.

Pixel analysis is streaming: Python holds one decoded frame and bounded NumPy
tiles, rather than all frames' RGB pixels. This is not an end-to-end constant
memory guarantee. dovi_tool reads the complete generator configuration and can
build a complete list of RPU metadata during generation/export, so its metadata
memory use grows with clip length.

RPU verification checks every frame's L1 values and the total count, and checks
the composer's fingerprint only on the first frame. Reports count these scopes
separately; they do not claim complete composer or other-metadata verification
for every frame. Raw generated/final RPU hashes can differ because a muxer may
reorder extension blocks and update their CRCs without changing their meaning.

HDR-Analyze's MIT reference implementation was consulted for composer processing,
native 4:2:0 luma downsampling and chroma interpolation. This NumPy implementation
uses a different normalization, clamp policy and full color transform; it is not
that tool's analysis algorithm. [Reference source](https://github.com/tinof/hdr-analyze/blob/v0.8.0/hdr_analyzer_mvp/src/analysis/hlg.rs).

## HDR-Analyze license notice

MIT License

Copyright (c) 2025 HDR-Analyze Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
