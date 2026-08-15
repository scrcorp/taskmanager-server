#!/usr/bin/env python
"""VAPID 키 쌍 생성기 — 웹 푸시용.

    python scripts/gen_vapid_keys.py

출력을 그대로 해당 환경의 .env 에 붙여 넣는다.

⚠️ 구독은 "구독 당시의 공개키" 에 묶인다. 이미 운영 중인 환경의 키를 이 스크립트로
   새로 만들어 갈아끼우면 기존 구독이 **에러 없이** 전부 무효가 되어 전 직원이
   재설치·재허용해야 한다. prod 키는 한 번 정하면 바꾸지 말 것.
   환경(dev/staging/prod)별로 서로 다른 키 쌍을 쓴다.

형식: P-256(secp256r1). 공개키 = 비압축 포인트 65바이트, 개인키 = 스칼라 32바이트.
      둘 다 base64url(패딩 제거) — 각각 87자 / 43자가 나온다.
      pywebpush 와 브라우저 applicationServerKey 가 기대하는 형식이다.
"""
import base64

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    private = key.private_numbers().private_value.to_bytes(32, "big")

    print("VAPID_PUBLIC_KEY=" + _b64url(public))
    print("VAPID_PRIVATE_KEY=" + _b64url(private))
    print("VAPID_SUBJECT=mailto:hello@tigersplus.com")


if __name__ == "__main__":
    main()
