# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
性能测试数据生成器

主要功能：
1. 生成 sonnet 文本数据集
2. 生成测试图片数据集
3. 支持多种图片尺寸
4. 自动去重和错误处理
"""

import argparse
import hashlib
import logging
from pathlib import Path
from io import BytesIO

import requests
import numpy as np
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── 带重试的 Session ───────────────────────────────────
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
session = requests.Session()
retry = Retry(
    total=5,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)


def create_sonnet_dataset(output_path: str = "./sonnet_20x.txt", num_lines: int = None) -> str:
    """创建指定行数的 sonnet 数据集文件
    
    Args:
        output_path: 输出文件路径
        num_lines: 需要生成的行数，如果为None则使用默认的16行
    
    Returns:
        生成的文本文件路径
    """
    sample_data = [
        "Shall I compare thee to a summer's day?\n",
        "Thou art more lovely and more temperate:\n",
        "Rough winds do shake the darling buds of May,\n",
        "And summer's lease hath all too short a date:\n",
        "Sometime too hot the eye of heaven shines,\n",
        "And often is his gold complexion dimm'd;\n",
        "And every fair from fair sometime declines,\n",
        "By chance or nature's changing course untrimm'd;\n",
        "But thy eternal summer shall not fade,\n",
        "Nor lose possession of that fair thou ow'st;\n",
        "Nor shall death brag thou wander'st in his shade,\n",
        "When in eternal lines to time thou grow'st:\n",
        "   So long as men can breathe or eyes can see,\n",
        "   So long lives this, and this gives life to thee.\n",
        "The world is too much with us; late and soon,\n",
        "Getting and spending, we lay waste our powers:\n",
    ]
    
    # 如果未指定行数，使用默认的完整样本
    if num_lines is None:
        lines_to_write = sample_data
    else:
        # 计算需要重复多少次完整样本
        full_repeats = num_lines // len(sample_data)
        remainder = num_lines % len(sample_data)
        
        # 生成指定行数的文本
        lines_to_write = sample_data * full_repeats + sample_data[:remainder]
    
    # 确保目录存在
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines_to_write)
    
    logger.info(f"Sonnet dataset created with {len(lines_to_write)} lines: {output_path}")
    return str(output_path)


def fetch_random_image(h: int, w: int) -> Image.Image:
    """直接从 picsum.photos 拉取随机图并返回 PIL.Image"""
    try:
        resp = session.get(
            f"https://picsum.photos/{h}/{w}",
            timeout=10,
            verify=False
        )
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content))
    except requests.RequestException as e:
        raise Exception(f"Failed to download image: {e}") from e
    except (IOError, OSError) as e:
        raise Exception(f"Failed to process image data: {e}") from e


def image_hash(img: Image.Image) -> str:
    """像素级 MD5 哈希，用于去重"""
    arr = np.array(img)
    return hashlib.md5(arr.tobytes()).hexdigest()


def generate_image_dataset(
    n: int = 20, 
    height: int = 1000, 
    width: int = 1000, 
    base_dir: str = "images",
    image_format: str = "png"
) -> str:
    """
    生成测试图片数据集
    
    Args:
        n: 需要生成的图片数量
        height: 图片高度
        width: 图片宽度
        base_dir: 基础目录
        image_format: 图片格式 (png, jpg, etc.)
    
    Returns:
        生成的图片目录路径
    """
    # 在 base_dir 下创建子目录 images_{height}x{width}
    outdir = Path(base_dir) / f"images_{height}x{width}"
    outdir.mkdir(parents=True, exist_ok=True)

    seen = set()
    attempts = 0
    max_attempts = n * 10  # 最多尝试10倍数量

    # 只要 collected < n，就一直循环
    while len(seen) < n and attempts < max_attempts:
        attempts += 1
        try:
            img = fetch_random_image(height, width)
        except requests.RequestException as e:
            logger.warning(f"⚠️ Attempt {attempts} failed to download: {e}")
            continue
        except IOError as e:
            logger.warning(f"⚠️ Attempt {attempts} failed to process image: {e}")
            continue
        except Exception as e:
            logger.error(f"⚠️ Attempt {attempts} encountered unknown error: {e}")
            continue
        hval = image_hash(img)
        if hval in seen:
            logger.info(f"⚠️ Attempt {attempts} produced duplicate image, hash={hval}")
            continue


        # 新图片，保存之
        idx = len(seen) + 1
        seen.add(hval)
        filename = f"{idx:03d}_{hval}.{image_format}"
        path = outdir / filename
        img.save(path)
        logger.info(f"✅ Saved image {idx}/{n} → {path}")
    logger.info(f"\nTried {attempts} times, successfully saved {len(seen)} unique images to '{outdir}/'")
    return str(outdir)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Performance test data generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 生成 sonnet 文本数据集 (默认10000行)
  python data_generator.py --type text --output ./sonnet_20x.txt
  
  # 生成指定行数的 sonnet 文本数据集
  python data_generator.py --type text --output ./sonnet_100x.txt --text-lines 100
  
  # 生成图片数据集
  python data_generator.py --type image --n 50 --height 512 --width 512
        """
    )
    
    parser.add_argument('--type', type=str, choices=['text', 'image', 'multimodal'], 
                    default='multimodal', help='Type of data to generate')
    parser.add_argument('--output', type=str, default="./sonnet_20x.txt", 
                    help='Output path for the text dataset')
    parser.add_argument('--text-lines', type=int, default=10000, 
                    help='Number of lines to generate for text dataset')
    parser.add_argument('--image-base-dir', type=str, default="./images", 
                    help='Base directory for images')
    parser.add_argument('--n', type=int, default=20, help='Number of images to generate')
    parser.add_argument('--height', type=int, default=1000, help='Height of the images')
    parser.add_argument('--width', type=int, default=1000, help='Width of the images')
    parser.add_argument('--image-format', type=str, default='png', choices=['png', 'jpg'], 
                    help='Image format')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing files')

    args = parser.parse_args()

    try:
        if args.type == 'text':
            create_sonnet_dataset(args.output, args.text_lines)
            
        elif args.type == 'image':
            generate_image_dataset(
                n=args.n,
                height=args.height,
                width=args.width,
                base_dir=args.image_base_dir,
                image_format=args.image_format
            )
                
    except Exception as e:
        logger.error(f"Data generation failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
