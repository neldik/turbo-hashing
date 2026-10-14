from importlib.metadata import version

__version__ = version("pkg-b")

from pkg_a import greet


def shout(name: str) -> str:
    return greet(name).upper() + " (via pkg-b)"
