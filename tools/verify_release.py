"""검증된 릴리스 실행 파일의 SHA-256을 확인한다."""
import argparse
import hashlib
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('artifact', type=Path)
    parser.add_argument('expected', type=Path)
    args = parser.parse_args()
    actual = sha256(args.artifact)
    expected = args.expected.read_text(encoding='utf-8').split()[0].upper()
    if actual != expected:
        raise SystemExit(f'SHA256 mismatch: expected {expected}, got {actual}')
    print(f'SHA256 OK {actual} {args.artifact}')


if __name__ == '__main__':
    main()
