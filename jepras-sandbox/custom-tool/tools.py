"""Custom tools for the sandbox custom-tool example."""


async def shout(text: str) -> str:
    """Return the given text uppercased with three exclamation marks."""
    return f"{text.upper()}!!!"


async def word_count(text: str) -> str:
    """Return the number of words in the given text."""
    return f"{len(text.split())} words"
