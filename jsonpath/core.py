"""
============================
:mod:`core` -- JSONPath Core
============================
"""
# Standard Library
import functools
import json
import weakref

from abc import abstractmethod
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from weakref import ReferenceType

# Third Party Library
from typing_extensions import Literal

var_root: ContextVar[Any] = ContextVar("root")
var_parent: ContextVar[Union[List[Any], Dict[str, Any]]] = ContextVar("parent")
T_SELF_VALUE = Union[Tuple[int, Any], Tuple[str, Any]]
var_self: ContextVar[T_SELF_VALUE] = ContextVar("self")
var_finding: ContextVar[bool] = ContextVar("finding", default=False)
T_VALUE = Union[int, float, str, Literal[None], Literal[True], Literal[False]]

T = TypeVar("T", bound="Expr")


class JSONPathError(Exception):
    """
    JSONPath Base Exception.
    """


class JSONPathSyntaxError(JSONPathError, SyntaxError):
    """
    JSONPath expression syntax error.

    :param expr: JSONPath expression
    :type expr: str
    """

    def __init__(self, expr: str):
        self.expr = expr
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"{self.expr!r} is not a valid JSONPath expression."

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.expr!r})"


class JSONPathUndefinedFunctionError(JSONPathError):
    """
    Undefined Function in JSONPath expression error.
    """


class JSONPathFindError(JSONPathError):
    """
    JSONPath executable object finds nothing.
    """


@contextmanager
def temporary_set(
    context_var: ContextVar[Any], value: Any
) -> Generator[None, None, None]:
    """
    Set the context variable temporarily via the 'with' statement.

    >>> var_boo = ContextVar("boo")
    >>> with temporary_set(var_boo, True):
    ...     assert var_boo.get() is True
    >>> var_boo.get()
    Traceback (most recent call last):
        ...
    LookupError: ...
    """
    token = context_var.set(value)
    try:
        yield
    finally:
        context_var.reset(token)


def _dfs_find(expr: "Expr", elements: List[Any]) -> Generator[Any, None, None]:
    """
    use DFS to find all target elements.
    the next expr finds in the result found by the current expr.
    """
    next_expr = expr.get_next()
    for element in elements:
        try:
            found_elements = expr.find(element)
        except JSONPathFindError:
            continue

        if not found_elements:
            continue

        if next_expr is None:
            # collect all found elements if there is no next expr.
            yield from found_elements
            continue

        with temporary_set(var_parent, element):
            yield from _dfs_find(next_expr, found_elements)


class ExprMeta(type):
    """
    JSONPath Expr Meta Class.
    """

    _classes: Dict[str, "ExprMeta"] = {}

    def __new__(
        metacls, name: str, bases: Tuple[type], attr_dict: Dict[str, Any]
    ) -> "ExprMeta":
        if "find" not in attr_dict:
            return _create_expr_cls(metacls, name, bases, attr_dict)

        actual_find = attr_dict["find"]

        @functools.wraps(actual_find)
        def find(self: "Expr", element: Any) -> List[Any]:
            if var_finding.get():
                # the chained expr in the finding process
                try:
                    return actual_find(self, element)
                except JSONPathFindError:
                    if self.ref_begin is None:
                        raise

                    return []

            return list(self.find_iter(element))

        attr_dict["find"] = find
        return _create_expr_cls(metacls, name, bases, attr_dict)


def _create_expr_cls(
    metacls: Type[ExprMeta],
    name: str,
    bases: Tuple[type],
    attr_dict: Dict[str, Any],
) -> ExprMeta:
    cls = type.__new__(metacls, name, bases, attr_dict)
    if name != "Expr":
        # not registers the base class
        metacls._classes[name] = cls

    return cls


class Expr(metaclass=ExprMeta):
    """
    JSONPath executable Class.

    .. automethod:: find
    .. automethod:: find_first
    .. automethod:: find_iter
    .. automethod:: get_expression
    .. automethod:: get_begin
    .. automethod:: get_next
    .. automethod:: chain
    .. automethod:: __getattr__
    """

    def __init__(self) -> None:
        self.left: Optional[Expr] = None
        self.ref_right: Optional[ReferenceType[Expr]] = None
        self.ref_begin: Optional[ReferenceType[Expr]] = None

    def __repr__(self) -> str:
        args = [self.get_expression(), self._get_partial_expression()]
        right = self.ref_right and self.ref_right()
        if right:
            args.append(right._get_partial_expression())

        return f"JSONPath({', '.join(map(repr, args))})"

    def get_expression(self) -> str:
        """
        Get full JSONPath expression.
        """
        expr: Optional[Expr] = self.get_begin()
        parts: List[str] = []
        while expr:
            part = expr._get_partial_expression()
            if isinstance(expr, (Array, Predicate, Search, Compare)):
                if parts:
                    parts[-1] += part
                else:
                    parts.append(part)
            else:
                parts.append(part)

            expr = expr.get_next()

        return ".".join(parts)

    @abstractmethod
    def _get_partial_expression(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def find(self, element: Any) -> List[Any]:
        """
        Find target data by the JSONPath expression.

        :param element: Root data where target data found from
        :type element: Any

        :returns: A list of target data
        :rtype: List[Any]
        """
        raise NotImplementedError

    def find_first(self, element: Any) -> Any:
        """
        Find first target data by the JSONPath expression.

        :param element: Root data where target data found from
        :type element: Any

        :returns: the first target data
        :rtype: Any
        :raises JSONPathFindError: Found nothing
        """
        notfound = object()
        rv = next(self.find_iter(element), notfound)
        if rv is notfound:
            raise JSONPathFindError("Found nothing")

        return rv

    def find_iter(self, element: Any) -> Generator[Any, None, None]:
        """
        Iterable find target data by the JSONPath expression.

        :param element: Root data where target data found from
        :type element: Any

        :returns: the generator of target data list
        :rtype: Generator[Any, None, None]
        """
        # the chained expr begins to find
        begin = self.get_begin()

        token_root = None
        try:
            var_root.get()
        except LookupError:
            # set the root element when the chained expr begins to find.
            # the partial exprs of the nested expr
            # can execute find method many times
            # but only the first time finding can set the root element.
            token_root = var_root.set(element)

        try:
            with temporary_set(var_finding, True):
                yield from _dfs_find(begin, [element])
        finally:
            if token_root:
                var_root.reset(token_root)

    def get_begin(self) -> "Expr":
        """
        Get the begin expr of the combined expr.

        :returns: The begin expr of the combined expr.
        :rtype: :class:`jsonpath.core.Expr`
        """
        if self.ref_begin is None:
            # the unchained expr's ref_begin is None
            return self
        else:
            begin = self.ref_begin()
            assert begin, "the chained expr must have a beginning expr"
            return begin

    def get_next(self) -> Optional["Expr"]:
        """
        Get the next part of expr in the combined expr.

        :returns: The next part of expr in the combined expr.
        :rtype: :class:`jsonpath.core.Expr`
        """
        return self.ref_right() if self.ref_right else None

    def chain(self, next_expr: T) -> T:
        """
        Chain the next part of expr as a combined expr.

        :param next_expr: The next part of expr in the combined expr.
        :type next_expr: :class:`jsonpath.core.Expr`

        :returns: The next part of expr in the combined expr.
        :rtype: :class:`jsonpath.core.Expr`
        """
        if self.ref_begin is None:
            # the unchained expr become the first expr in chain
            self.ref_begin = weakref.ref(self)

        next_expr.ref_begin = self.ref_begin
        # it return next expr,
        # so need to keep the current expr's ref into next expr's left.
        # keeping the next expr's weak ref into current expr's ref_right
        # helps to find target elements in sequential.
        next_expr.left = self
        self.ref_right = weakref.ref(next_expr)
        return next_expr

    def __getattr__(self, name: str) -> Callable[..., "Expr"]:
        """
        Create combined expr in a serial of chain class creations
        like `Root().Name("*")`.

        :param name: The name of the next part expr in combined expr.
        :type name: str

        :returns: The function for creating the next part of the combined expr.
        :rtype: Callable[[...], :class:`Expr`]

        :raises AttributeError: The name of Expr Component not found
        """
        if name not in Expr._classes:
            return super().__getattribute__(name)  # for raising AttributeError

        cls = Expr._classes[name]

        def cls_(*args: Any, **kwargs: Any) -> Expr:
            expr = cls(*args, **kwargs)
            return self.chain(next_expr=expr)

        return cls_

    def __lt__(self, value: Any) -> "Expr":
        return self.LessThan(value)

    def __le__(self, value: Any) -> "Expr":
        return self.LessEqual(value)

    def __eq__(self, value: Any) -> "Expr":  # type: ignore
        return self.Equal(value)

    def __ge__(self, value: Any) -> "Expr":
        return self.GreaterEqual(value)

    def __gt__(self, value: Any) -> "Expr":
        return self.GreaterThan(value)

    def __ne__(self, value: Any) -> "Expr":  # type: ignore
        return self.NotEqual(value)


class Value(Expr):
    """
    Represent the value in the expression.

    It is used mainly to support parsing comparison expression
    which value is on the left.

    >>> p = Value("boo"); print(p.get_expression())
    "boo"
    >>> p.find([])
    ['boo']
    >>> print(Value(True).get_expression())
    true
    >>> print(Value(False).get_expression())
    false
    >>> print(Value(None).get_expression())
    null
    >>> print(Value(1).get_expression())
    1
    >>> print(Value(1.1).get_expression())
    1.1
    >>> print(Value(1).LessThan(Value(2)).get_expression())
    1 < 2

    """

    def __init__(self, value: T_VALUE) -> None:
        super().__init__()
        self.value = value

    def _get_partial_expression(self) -> str:
        return json.dumps(self.value)

    def find(self, element: Any) -> List[Any]:
        return [self.value]


class Root(Expr):
    """
    Represent the root of data.

    >>> p = Root(); print(p.get_expression())
    $
    >>> p.find([1])
    [[1]]

    """

    def _get_partial_expression(self) -> str:
        return "$"

    def find(self, element: Any) -> List[Any]:
        return [var_root.get()]


class Name(Expr):
    """
    Represent the data of the field name.
    Represent the data of all fields if not providing the field name.

    :param name: The field name of the data.
    :type name: Optional[str]

    >>> p = Name("abc"); print(p.get_expression())
    abc
    >>> p.find({"abc": 1})
    [1]
    >>> p = Name(); print(p.get_expression())
    *
    >>> p.find({"a": 1, "b": 2})
    [1, 2]
    >>> p = Name("a").Name("b"); print(p.get_expression())
    a.b
    >>> p.find({"a": {"b": 1}})
    [1]

    """

    def __init__(self, name: Optional[str] = None) -> None:
        super().__init__()
        self.name = name

    def _get_partial_expression(self) -> str:
        if self.name is None:
            return "*"

        name = self.name
        if name in ("*", "$", "@"):
            name = repr(name)

        return name

    def find(self, element: Any) -> List[Any]:
        if not isinstance(element, dict):
            raise JSONPathFindError

        if self.name is None:
            return list(element.values())

        if self.name not in element:
            raise JSONPathFindError

        return [element[self.name]]


class Array(Expr):
    """
    Represent the array data
    if combine with expr (e.g., :class:`Name`, :class:`Root`) as the next part.

    Use an array index to get the item of the array.

    >>> p = Root().Array(0); print(p.get_expression())
    $[0]
    >>> p.find([1, 2])
    [1]

    Also can use a :class:`Slice` to get partial items of the array.

    >>> p = Root().Array(Slice(0, 3, 2)); print(p.get_expression())
    $[:3:2]
    >>> p.find([1, 2, 3, 4])
    [1, 3]

    Accept :data:`None` to get all items of the array.

    >>> p = Root().Array(); print(p.get_expression())
    $[*]
    >>> p.find([1, 2, 3, 4])
    [1, 2, 3, 4]

    """

    def __init__(self, idx: Optional[Union[int, "Slice"]] = None) -> None:
        super().__init__()
        assert idx is None or isinstance(idx, (int, Slice)), (
            '"idx" parameter must be an instance of the "int" or "Slice" class,'
            ' or "None" value'
        )
        self.idx = idx

    def _get_partial_expression(self) -> str:
        if self.idx is None:
            return "[*]"
        else:
            idx_str = (
                self.idx.get_expression() if isinstance(self.idx, Expr) else self.idx
            )
            return f"[{idx_str}]"

    def find(self, element: Any) -> List[Any]:
        if isinstance(element, list):
            if self.idx is None:
                return element
            elif isinstance(self.idx, int):
                with suppress(IndexError):
                    return [element[self.idx]]
            elif isinstance(self.idx, Slice):
                return self.idx.find(element)
            else:
                raise AssertionError(f"self.idx={self.idx!r} is not valid")

        raise JSONPathFindError


class Predicate(Expr):
    """
    Filter items from the array by expr(e.g., :class:`Compare`)
    if combine with expr (e.g., :class:`Name`, :class:`Root`) as the next part.
    It is also able to process dictionary.

    Accept comparison expr for filtering.
    See more in :class:`Compare`.

    >>> p = Root().Predicate(Name("a") == 1); print(p.get_expression())
    $[a = 1]
    >>> p.find([{"a": 1}, {"a": 2}, {}])
    [{'a': 1}]
    >>> p = Root().Predicate(Contains(Key(), "a")); print(p.get_expression())
    $[contains(key(), "a")]
    >>> p.find({"a": 1, "ab": 2, "c": 3})
    [1, 2]

    Or accept single expr for filtering.

    >>> p = Root().Predicate(Name("a")); print(p.get_expression())
    $[a]
    >>> p.find([{"a": 0}, {"a": 1}, {}])
    [{'a': 1}]
    """

    def __init__(self, expr: Union["Compare", Expr]) -> None:
        super().__init__()
        assert isinstance(
            expr, Expr
        ), '"expr" parameter must be an instance of the "Expr" class.'
        self.expr = expr

    def _get_partial_expression(self) -> str:
        return f"[{self.expr.get_expression()}]"

    def find(self, element: Union[List[Any], Dict[str, Any]]) -> List[Any]:
        filtered_items = []
        items: Union[Iterator[tuple[str, Any]], Iterator[tuple[int, Any]]]
        if isinstance(element, list):
            items = iter(enumerate(element))
        elif isinstance(element, dict):
            items = iter(element.items())
        else:
            raise JSONPathFindError

        for item in items:
            # save the current item into var_self for Self()
            with temporary_set(var_self, item):
                _, value = item
                # set var_finding False to
                # start new finding process for the nested expr: self.idx
                with temporary_set(var_finding, False):
                    rv = self.expr.find(value)

                if rv and rv[0]:
                    filtered_items.append(value)

        return filtered_items


class Slice(Expr):
    """
    Use it with :class:`Array` to get partial items from the array data.
    Work like the `Python slice(range)`_.

    .. _Python slice(range): https://docs.python.org/3/library/stdtypes.html#ranges
    """

    def __init__(
        self,
        start: Union[Expr, int, None] = None,
        stop: Union[Expr, int, None] = None,
        step: Union[Expr, int, None] = None,
    ) -> None:
        super().__init__()
        self.start = start
        self.end = stop
        self.step = step

    def _get_partial_expression(self) -> str:
        parts = []
        if self.start:
            parts.append(
                self.start.get_expression()
                if isinstance(self.start, Expr)
                else str(self.start)
            )
        else:
            parts.append("")

        if self.end:
            parts.append(
                self.end.get_expression()
                if isinstance(self.end, Expr)
                else str(self.end)
            )
        else:
            parts.append("")

        if self.step:
            parts.append(
                self.step.get_expression()
                if isinstance(self.step, Expr)
                else str(self.step)
            )

        return ":".join(parts)

    def _ensure_int_or_none(self, value: Union[Expr, int, None]) -> Union[int, None]:
        if isinstance(value, Expr):
            # set var_finding False to start new finding process for the nested expr
            with temporary_set(var_finding, False):
                found_elements = value.find(var_parent.get())
            if not found_elements or not isinstance(found_elements[0], int):
                raise JSONPathFindError
            return found_elements[0]
        else:
            return value

    def find(self, element: List[Any]) -> List[Any]:
        assert isinstance(element, list), "Slice.find apply on list only."

        start = self._ensure_int_or_none(self.start)
        end = self._ensure_int_or_none(self.end)
        step = self._ensure_int_or_none(self.step)

        if start is None:
            start = 0
        if end is None:
            end = len(element)
        if step is None:
            step = 1

        return element[start:end:step]


class Brace(Expr):
    """
    Brace groups part of expression,
    uses sub-expression to find the target data,
    and wraps the found result as an array.

    >>> p = Root().Array().Name("a"); print(p.get_expression())
    $[*].a
    >>> p.find([{"a": 1}])
    [1]

    >>> p = Brace(p); print(p.get_expression())
    ($[*].a)
    >>> p.find([{"a": 1}])
    [[1]]

    It seems to be useless but makes chaining filtering become possible.
    The expressions like `"$[@ < 100][@ >= 50]"` can not perform chaining filtering.
    Because the Predicate (and Array) class always unpacks the found elements to
    avoid the found result looking like `[[[[[[[...]]]]]]]`.
    So the right way to do chaining filter is that it should use with Brace class.

    >>> p = Brace(Root().Predicate(Self() < 100)).Predicate(Self() >= 50)
    >>> print(p.get_expression())
    ($[@ < 100])[@ >= 50]
    >>> p.find([100, 99, 50, 1])
    [99, 50]

    Generally, we will use And expression do that. e.g. `"$[@ < 100 and @ >= 50]"`

    >>> p = Brace(
    ...     Root().Array().Name("a")
    ... ).Predicate(Self() == 1)
    >>> print(p.get_expression())
    ($[*].a)[@ = 1]
    >>> p.find([{"a": 1}, {"a": 2}, {"a": 1}, {}])
    [1, 1]

    """

    def __init__(self, expr: Expr) -> None:
        super().__init__()
        assert isinstance(
            expr, Expr
        ), '"expr" parameter must be an instance of the "Expr" class.'
        self._expr = expr

    def _get_partial_expression(self) -> str:
        return f"({self._expr.get_expression()})"

    def find(self, element: Any) -> List[Any]:
        # set var_finding False to
        # start new finding process for the nested expr: self.expr
        with temporary_set(var_finding, False):
            return [self._expr.find(element)]


def _recursive_find(expr: Expr, element: Any, rv: List[Any]) -> None:
    """
    recursive find in every node.
    """
    try:
        find_rv = expr.find(element)
        rv.extend(find_rv)
    except JSONPathFindError:
        pass

    with temporary_set(var_parent, element):
        if isinstance(element, list):
            for item in element:
                _recursive_find(expr, item, rv)
        elif isinstance(element, dict):
            for item in element.values():
                _recursive_find(expr, item, rv)


class Search(Expr):
    """
    Recursively search target in data.

    :param expr: The expr is used to search in data recursively.
    :type expr: :class:`Expr`

    >>> p = Root().Search(Name("a")); print(p.get_expression())
    $..a
    >>> p.find({"a":{"a": 0}})
    [{'a': 0}, 0]

    """

    def __init__(self, expr: Expr) -> None:
        super().__init__()
        assert isinstance(
            expr, Expr
        ), '"expr" parameter must be an instance of the "Expr" class.'
        # TODO: Not accepts mixed expr
        self._expr = expr

    def _get_partial_expression(self) -> str:
        return f"..{self._expr.get_expression()}"

    def find(self, element: Any) -> List[Any]:
        rv: List[Any] = []
        if isinstance(self._expr, Predicate):
            # filtering find needs to begin on the current element
            _recursive_find(self._expr, [element], rv)
        else:
            _recursive_find(self._expr, element, rv)
        return rv


class Self(Expr):
    """
    Represent each item of the array data.

    >>> p = Root().Predicate(Self()==1); print(p.get_expression())
    $[@ = 1]
    >>> p.find([1, 2, 1])
    [1, 1]

    """

    def _get_partial_expression(self) -> str:
        return "@"

    def find(self, element: Any) -> List[Any]:
        try:
            _, value = var_self.get()
            return [value]
        except LookupError:
            return [element]


class Compare(Expr):
    """
    Base class of comparison operators.

    Compare value between the first result of an expression,
    and the first result of an expression or simple value.

    >>> Root().Predicate(Self() == 1)
    JSONPath('$[@ = 1]', '[@ = 1]')
    >>> Root().Predicate(Self().Equal(1))
    JSONPath('$[@ = 1]', '[@ = 1]')
    >>> Root().Predicate(Self() <= 1)
    JSONPath('$[@ <= 1]', '[@ <= 1]')
    >>> (
    ...     Root()
    ...     .Name("data")
    ...     .Predicate(
    ...         Self() != Root().Name("target")
    ...     )
    ... )
    JSONPath('$.data[@ != $.target]', '[@ != $.target]')

    """

    def __init__(self, target: Any) -> None:
        super().__init__()
        self.target = target

    def _get_target_expression(self) -> str:
        if isinstance(self.target, Expr):
            return self.target.get_expression()
        else:
            return json.dumps(self.target)

    def get_target_value(self) -> Any:
        if isinstance(self.target, Expr):
            # set var_finding False to
            # start new finding process for the nested expr: self.target
            with temporary_set(var_finding, False):
                # multiple exprs begins on self-value in filtering find,
                # except the self.target expr starts with root-value.
                _, value = var_self.get()
                rv = self.target.find(value)
                if not rv:
                    raise JSONPathFindError

                return rv[0]
        else:
            return self.target


class LessThan(Compare):
    def _get_partial_expression(self) -> str:
        return f" < {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element < self.get_target_value()]


class LessEqual(Compare):
    def _get_partial_expression(self) -> str:
        return f" <= {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element <= self.get_target_value()]


class Equal(Compare):
    def _get_partial_expression(self) -> str:
        return f" = {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element == self.get_target_value()]


class GreaterEqual(Compare):
    def _get_partial_expression(self) -> str:
        return f" >= {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element >= self.get_target_value()]


class GreaterThan(Compare):
    def _get_partial_expression(self) -> str:
        return f" > {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element > self.get_target_value()]


class NotEqual(Compare):
    def _get_partial_expression(self) -> str:
        return f" != {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element != self.get_target_value()]


class And(Compare):
    """
    And, a boolean operator.

    """

    def _get_partial_expression(self) -> str:
        return f" and {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element and self.get_target_value()]


class Or(Compare):
    """
    Or, a boolean operator.

    """

    def _get_partial_expression(self) -> str:
        return f" or {self._get_target_expression()}"

    def find(self, element: Any) -> List[bool]:
        return [element or self.get_target_value()]


def _get_expression(target: Any) -> str:
    if isinstance(target, Expr):
        return target.get_expression()
    else:
        return json.dumps(target)


class Function(Expr):
    """
    Base class of functions.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__()
        self.args = args

    @abstractmethod
    def find(self, element: Any) -> List[Any]:
        raise NotImplementedError


class Key(Function):
    """
    Key function is used to get the field name from dictionary data.

    >>> Root().Predicate(Key() == "a")
    JSONPath('$[key() = "a"]', '[key() = "a"]')

    Same as :data:`Root().Name("a")`.

    Filter all values which field name contains :data:`"book"`.

    >>> p = Root().Predicate(Contains(Key(), "book"))
    >>> print(p.get_expression())
    $[contains(key(), "book")]
    >>> p.find({"book 1": 1, "picture 2": 2})
    [1]

    """

    def __init__(self, *args: List[Any]) -> None:
        super().__init__(*args)
        assert not self.args

    def _get_partial_expression(self) -> str:
        return "key()"

    def find(self, element: Any) -> List[Union[int, str]]:
        # Key.find only executed in the predicate.
        # So Array.find being executed first that set the var_self
        key, _ = var_self.get()
        return [key]


class Contains(Function):
    """
    Determine the first result of expression contains the target substring.

    >>> p = Root().Predicate(Contains(Name("name"), "red"))
    >>> print(p.get_expression())
    $[contains(name, "red")]
    >>> p.find([
    ...     {"name": "red book"},
    ...     {"name": "red pen"},
    ...     {"name": "green book"}
    ... ])
    [{'name': 'red book'}, {'name': 'red pen'}]

    Check the specific key in the dictionary.

    >>> p = Root().Predicate(Contains(Self(), "a"))
    >>> print(p.get_expression())
    $[contains(@, "a")]
    >>> p.find([{"a": 0}, {"a": 1}, {}, {"b": 1}])
    [{'a': 0}, {'a': 1}]

    """

    def __init__(self, expr: Expr, target: Any, *args: List[Any]) -> None:
        super().__init__(expr, target, *args)
        assert isinstance(
            expr, Expr
        ), '"expr" parameter must be an instance of the "Expr" class.'
        assert not args
        self._expr = expr
        self._target = target

    def _get_partial_expression(self) -> str:
        args_list = f"{_get_expression(self._expr)}, {_get_expression(self._target)}"
        return f"contains({args_list})"

    def find(self, element: Any) -> List[bool]:
        rv = self._expr.find(element)
        if not rv:
            return []
        root_arg = rv[0]
        target_arg = self._target
        if isinstance(target_arg, Expr):
            # set var_finding False to
            # start new finding process for the nested expr: target_arg
            with temporary_set(var_finding, False):
                rv = self._target.find(element)

            if not rv:
                return []

            # use the first value of results as target
            target_arg = rv[0]

        return [target_arg in root_arg]


class Not(Function):
    """
    Not, a boolean operator.

    >>> Root().Predicate(Not(Name("enable")))
    JSONPath('$[not(enable)]', '[not(enable)]')

    """

    def __init__(self, expr: Expr, *args: List[Any]) -> None:
        super().__init__(expr, *args)
        assert not args
        assert isinstance(
            expr, Expr
        ), '"expr" parameter must be an instance of the "Expr" class.'
        self._expr = expr

    def _get_partial_expression(self) -> str:
        return f"not({self._expr.get_expression()})"

    def find(self, element: Any) -> List[bool]:
        # set var_finding False to
        # start new finding process for the nested expr: target
        with temporary_set(var_finding, False):
            rv = self._expr.find(element)

        return [not v for v in rv]


__all__ = (
    "And",
    "Array",
    "Brace",
    "Compare",
    "Contains",
    "Equal",
    "Expr",
    "ExprMeta",
    "GreaterEqual",
    "GreaterThan",
    "Key",
    "LessEqual",
    "LessThan",
    "Name",
    "Not",
    "NotEqual",
    "Or",
    "Predicate",
    "Root",
    "Search",
    "Self",
    "Slice",
    "Value",
)
