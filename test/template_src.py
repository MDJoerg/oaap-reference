"""Read a template constant out of app.py WITHOUT importing it.

Several tests render a page template on any machine, without Flask and
requests, by reading the string constant from the source. They used
`ast.literal_eval`, which stopped working once a template was composed
from parts -- `INSTANCE_EDIT_BODY = \"\"\"...\"\"\" + CARE_CARD + IMAGE_CARD
+ \"\"\"...\"\"\"` (0.1.227/0.1.228): a name is not a literal, and six tests
had been breaking off ever since, each before its first check.

`resolve` follows exactly what composition needs: string constants,
`+`, and names of other module-level assignments. Anything else is still
handed to `literal_eval`, so a template that does something cleverer
fails loudly instead of being guessed at.
"""
import ast


def resolve(tree, node):
    consts = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = n.value

    def ev(v, seen=()):
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            return ev(v.left, seen) + ev(v.right, seen)
        if isinstance(v, ast.Name) and v.id in consts and v.id not in seen:
            return ev(consts[v.id], seen + (v.id,))
        return ast.literal_eval(v)

    return ev(node)
