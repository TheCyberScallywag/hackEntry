"""The brand the running service was started with (brand/brand.toml, or BRAND_DIR's)."""

from functools import cache

from app.brandkit import Brand, load


@cache
def get_meta() -> Brand:
    return load()
