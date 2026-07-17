#!/usr/bin/env python3
from openai_image_client.server import build_server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
