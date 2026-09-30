"""Errors written as prompts.

Every failure says what failed, what was expected, and the action that fixes
it. The reader is often an agent: a message it cannot act on costs a retry,
and one that suggests the wrong fix costs more.
"""


class Problem(Exception):
    def __init__(self, what: str, fix: str, expected: str = ""):
        super().__init__(what)
        self.what = what
        self.expected = expected
        self.fix = fix

    def render(self) -> str:
        lines = [f"usage-watch: {self.what}"]
        if self.expected:
            lines.append(f"  expected: {self.expected}")
        fix = self.fix.splitlines()
        lines.append(f"  fix: {fix[0]}")
        lines.extend(f"       {line}" for line in fix[1:])
        return "\n".join(lines)
