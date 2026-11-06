# Multi-piece CPU integration control

`photo_01.png` combines the generated `captures/piece_01.png` and `piece_02.png` at their original pixel sizes, with 96-pixel spacing and background padding. `photo_02.png` similarly combines pieces 03 and 04. No piece is resized or warped. `empty.png` is a 320×240 uniform image using the same background color.

The frozen v3 Stage 1 model must propose 2 + 2 + 0 pieces. The HTTP smoke control approves individual observations, explicitly confirms all three sources (including the empty source), and reconstructs four physical pieces. These are generated project fixtures, not private user photographs or ground-truth masks supplied to inference.
