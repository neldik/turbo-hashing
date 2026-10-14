from importlib.metadata import version

__version__ = version("pkg-a")


def greet(name: str) -> str:
    return f"hello {name} from pkg-a"
