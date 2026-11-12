"""Sandbox for mod source code.

Two layers:
1. Static AST gate: no imports, no dunder names/attributes, only a fixed
   whitelist of Python constructs. Compilation only happens after the gate
   passes, so `().__class__.__bases__` style escapes never reach eval.
2. Restricted globals: a small explicit builtin table and no module
   machinery. Mod code can only touch objects the host passes in.
"""

import ast
import signal

from .errors import LoadError, ModTimeoutExceeded


_ALLOWED_STMT = (
    ast.FunctionDef, ast.Return, ast.If, ast.For, ast.While, ast.Break,
    ast.Continue, ast.Pass, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.Expr, ast.Try, ast.Raise,
)

_ALLOWED_EXPR = (
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Lambda, ast.IfExp, ast.Dict,
    ast.Set, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.Compare, ast.Call, ast.Constant, ast.Name, ast.Attribute,
    ast.Subscript, ast.Starred, ast.List, ast.Tuple, ast.JoinedStr,
    ast.FormattedValue, ast.NamedExpr, ast.Slice,
)

_ALLOWED_MISC = (
    ast.Expression, ast.Module, ast.FunctionType, ast.arguments, ast.arg,
    ast.keyword, ast.comprehension, ast.ExceptHandler, ast.Load, ast.Store,
    ast.Del,
)

_ALLOWED_OPS = (
    ast.And, ast.Or, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.LShift, ast.RShift, ast.BitOr, ast.BitXor,
    ast.BitAnd, ast.MatMult, ast.Invert, ast.Not, ast.UAdd, ast.USub,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
)


_BLOCKED_NAMES = frozenset({
    "globals", "locals", "vars", "dir", "eval", "exec", "compile",
    "open", "input", "breakpoint", "memoryview", "property", "staticmethod",
    "classmethod", "super", "object", "type", "getattr", "setattr",
    "delattr", "hasattr", "__builtins__",
})


class _Gate(ast.NodeVisitor):
    def __init__(self):
        self.problem = None

    def _descend(self, node):
        for child in ast.iter_child_nodes(node):
            if self.problem:
                return
            self.visit(child)

    def generic_visit(self, node):
        if not isinstance(node, _ALLOWED_STMT + _ALLOWED_EXPR + _ALLOWED_MISC + _ALLOWED_OPS):
            self.problem = f"forbidden construct {type(node).__name__}"
            return
        self._descend(node)

    def visit_Name(self, node):
        if node.id.startswith("_"):
            self.problem = f"forbidden identifier {node.id!r}"
            return
        if node.id in _BLOCKED_NAMES:
            self.problem = f"forbidden builtin {node.id!r}"
            return
        self._descend(node)

    def visit_Attribute(self, node):
        if node.attr.startswith("_"):
            self.problem = f"forbidden attribute {node.attr!r}"
            return
        self._descend(node)

    def visit_FunctionDef(self, node):
        if node.name.startswith("_"):
            self.problem = f"forbidden function name {node.name!r}"
            return
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in all_args:
            if arg.arg.startswith("_"):
                self.problem = f"forbidden parameter name {arg.arg!r}"
                return
        if node.args.vararg and node.args.vararg.arg.startswith("_"):
            self.problem = "forbidden parameter name"
            return
        if node.args.kwarg and node.args.kwarg.arg.startswith("_"):
            self.problem = "forbidden parameter name"
            return
        self._descend(node)

    def visit_Import(self, node):
        self.problem = "import statements are forbidden"

    def visit_ImportFrom(self, node):
        self.problem = "import statements are forbidden"

    def visit_Global(self, node):
        self.problem = "global statements are forbidden"

    def visit_Nonlocal(self, node):
        self.problem = "nonlocal statements are forbidden"

    def visit_ClassDef(self, node):
        self.problem = "class definitions are forbidden"

    def visit_Yield(self, node):
        self.problem = "yield is forbidden"

    def visit_YieldFrom(self, node):
        self.problem = "yield is forbidden"

    def visit_Await(self, node):
        self.problem = "await is forbidden"


def compile_mod_source(source, mod_id):
    try:
        tree = ast.parse(source, filename=f"{mod_id}.py")
    except SyntaxError as exc:
        raise LoadError(f"[{mod_id}] syntax error: {exc}") from exc
    gate = _Gate()
    gate.visit(tree)
    if gate.problem:
        raise LoadError(f"[{mod_id}] rejected by sandbox gate: {gate.problem}")
    return compile(tree, filename=f"{mod_id}.py", mode="exec")


_SAFE_BUILTIN_NAMES = (
    "len", "range", "enumerate", "zip", "map", "filter", "sorted", "min",
    "max", "sum", "abs", "round", "any", "all", "isinstance", "tuple",
    "list", "dict", "set", "frozenset", "str", "int", "float", "bool",
    "repr", "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "ZeroDivisionError", "Exception", "StopIteration",
    "True", "False", "None",
)


def _resolve_builtin(name):
    builtins = __builtins__
    if isinstance(builtins, dict):
        return builtins[name]
    return getattr(builtins, name)


def safe_globals():
    return {"__builtins__": {name: _resolve_builtin(name) for name in _SAFE_BUILTIN_NAMES}}


class AlarmTimer:
    """SIGALRM wall-clock budget guard, nestable for reentrant hooks.

    The host installs the handler once for the level; arm() replaces the
    itimer with a shorter deadline and remembers the outer remaining time,
    disarm() restores it. Only usable on the main thread, which is where
    the host dispatches events.
    """

    def __init__(self):
        self._stack = []

    def install(self):
        signal.signal(signal.SIGALRM, self._on_alarm)

    def shutdown(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        self._stack.clear()
        signal.signal(signal.SIGALRM, signal.SIG_DFL)

    def arm(self, seconds):
        old_interval, old_remaining = signal.setitimer(
            signal.ITIMER_REAL, max(seconds, 0.001)
        )
        self._stack.append(old_remaining if old_remaining > 0 else None)

    def disarm(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        if not self._stack:
            return
        outer = self._stack.pop()
        if outer is not None and self._stack:
            signal.setitimer(signal.ITIMER_REAL, max(outer, 0.001))

    def _on_alarm(self, signum, frame):
        raise ModTimeoutExceeded()
