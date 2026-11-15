from PIL import Image, ImageDraw


S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=110, fill=(24, 24, 40, 255))
d.polygon([(91, 130), (91, 382), (311, 256)], fill=(45, 212, 191, 255))
d.rounded_rectangle((331, 150, 366, 362), radius=14, fill=(230, 230, 240, 255))
d.rounded_rectangle((386, 150, 421, 362), radius=14, fill=(230, 230, 240, 255))

img.save("icon.png")
img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])