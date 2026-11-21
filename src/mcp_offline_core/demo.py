"""Pure fixture tool for offline acceptance."""


def echo(arguments, _context):
    return {"echo": arguments["text"]}
