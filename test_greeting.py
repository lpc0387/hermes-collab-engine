import pytest

from hello import greet


def test_greet_with_name():
    """greet() should return a greeting with the given name."""
    result = greet("Alice")
    assert result == "Hello, Alice!"


def test_greet_with_empty_string():
    """greet() should handle an empty string."""
    result = greet("")
    assert result == "Hello, !"


def test_greet_with_special_characters():
    """greet() should handle names with special characters."""
    result = greet("Jean-Pierre O'Brien")
    assert result == "Hello, Jean-Pierre O'Brien!"


def test_greet_type_error():
    """greet() should raise TypeError when called without arguments."""
    with pytest.raises(TypeError):
        greet()  # type: ignore[call-arg]
