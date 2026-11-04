"""把 PDF 上传到阿里云 OSS 并生成短时签名 URL，供 Qwen PDF 理解使用 `file_url` 传输。

背景：DashScope 兼容模式对 `file_data`（base64）单字符串有约 28,000,000 字符上限，
大 PDF（如 27MB → base64 36MB）会被 400 拒绝。官方首选传输方式是短时签名 URL。

凭据来源（不写入本项目仓库）：
  - 环境变量 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET / OSS_ENDPOINT
  - 或 --env-file 指向的 env 文件（默认复用 ESGData-quantitative-skill/oss/.env）

用法：
  PYTHONPATH=. .venv/bin/python scripts/oss_signed_url.py --pdf "x.pdf" --ttl 7200
  PYTHONPATH=. .venv/bin/python scripts/oss_signed_url.py --delete oss-key
输出：JSON（stdout），含 bucket/key/url/expires_in；凭据不会打印。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import oss2

DEFAULT_ENV = Path("/Users/wujielin/ESG/ESGData-quantitative-skill/oss/.env")


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_bucket() -> oss2.Bucket:
    key_id = os.environ.get("OSS_ACCESS_KEY_ID") or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET") or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not key_id or not key_secret:
        raise SystemExit("缺少 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET")
    bucket_name = os.environ.get("OSS_BUCKET") or os.environ.get("OSS_BUCKET_NAME") or "hkubs-airi"
    endpoint = os.environ.get("OSS_ENDPOINT") or "oss-cn-shenzhen.aliyuncs.com"
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    auth = oss2.Auth(key_id, key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF → OSS 签名 URL / 删除临时对象")
    parser.add_argument("--pdf", type=Path, help="上传的 PDF 路径")
    parser.add_argument("--key", default=None, help="对象 key；默认 esgagent-tmp/<sha8>_<name>")
    parser.add_argument("--prefix", default="esgagent-tmp/")
    parser.add_argument("--ttl", type=int, default=7200, help="签名有效期（秒）")
    parser.add_argument("--delete", default=None, help="删除指定对象 key 后退出")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--keep", action="store_true", help="保留对象（默认保留；由调用方清理）")
    args = parser.parse_args(argv)

    load_env(args.env_file)
    bucket = get_bucket()

    if args.delete:
        bucket.delete_object(args.delete)
        print(json.dumps({"deleted": args.delete}, ensure_ascii=False))
        return 0

    if not args.pdf or not args.pdf.is_file():
        raise SystemExit(f"PDF 不存在: {args.pdf}")
    data = args.pdf.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = args.key or f"{args.prefix}{sha[:12]}_{args.pdf.name}"
    bucket.put_object(key, data, headers={"Content-Type": "application/pdf"})

    url = bucket.sign_url("GET", key, args.ttl)
    print(
        json.dumps(
            {
                "bucket": bucket.bucket_name,
                "key": key,
                "sha256": sha,
                "size_bytes": len(data),
                "expires_in": args.ttl,
                "url": url,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
