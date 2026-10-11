# 离线演示动图的录制脚本（vhs: https://github.com/charmbracelet/vhs）
#
# 这个脚本存在的理由：
#     README 里贴的输出是静态的，读者看不出「它真的能跑」。
#     而 GIF 一旦手改过，就跟代码里跑出来的对不上了 —— 所以录屏脚本入库，
#     任何人都能 `vhs scripts/demo.tape` 重新录一遍，录出来的必然是当前代码的真实输出。
#
# 只录 demo、不录 selftest 的理由：
#     selftest 那一屏 [PASS] 流水账是**开发者视角**的东西，路人看到只会
#     觉得「这堆东西有鸟用」。录屏是给路人看「能省多少钱」的，工程可信度
#     由 CI 徽章和「校验与开发」一节承担，别挤在一张图里。
#
# 用法（需先装 vhs、ttyd 与 ffmpeg）：
#     vhs scripts/demo.tape
#
# vhs 直出约 1MB，README 里用的要先压一遍（fps/尺寸/色板各降一档）：
#     ffmpeg -i screenshots/demo.gif -filter_complex "fps=12,scale=850:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" -loop 0 screenshots/demo.min.gif
#     mv screenshots/demo.min.gif screenshots/demo.gif
#
# 注意：这里必须录**离线**命令（demo），不能录需要 Token 的 ——
#     Token 一旦出现在录屏里就永久泄露，这是本项目最大的红线。

Output screenshots/demo.gif

Set Shell "powershell"
Set FontSize 14
Set Width 1000
Set Height 1010
Set Padding 24
Set Framerate 24
Set TypingSpeed 35ms
Set Theme "TokyoNight"

Type "python -m src.cli demo"
Sleep 400ms
Enter
Sleep 8s
