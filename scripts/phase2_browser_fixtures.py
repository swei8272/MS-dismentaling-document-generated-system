"""Create only synthetic boundary/failure images for real browser file selection."""
import argparse
from pathlib import Path
from PIL import Image

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for index in range(5):
        Image.new('RGB', (2300, 2300), (index * 40, 90, 130)).save(args.output / f'boundary-{index}.bmp')
    Image.new('RGB', (2400, 2400), (255, 30, 90)).save(args.output / 'oversize.bmp')
    for index in range(26):
        (args.output / f'damaged-{index:02d}.jpg').write_bytes(f'invalid synthetic image {index}'.encode())
    print('Created 5 valid boundary BMP, 1 oversized BMP, and 26 damaged JPEG fixtures')
