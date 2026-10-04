#!/usr/bin/env python3
"""Multilint Python test."""


def greet(name: str) -> str:
    return f"Hello, {name}!"


class Greeter:
    def __init__(self, name: str) -> None:
        self.name = name

    def say_hello(self) -> str:
        return greet(self.name)


if __name__ == "__main__":
    g = Greeter("World")
    print(g.say_hello())
