"""The nine tools that drive the browser, all of them one ``ViewerBus`` call.

Nothing here touches a socket, a registry or the filesystem. Each handler
validates its arguments, makes exactly one call into the primary viewer, and
returns what came back; the browser side of each method is the table in
``static/js/commands.js``, and the two files have to be read together, since
the method names and result shapes below are that table's contract.

Argument validation raises ``ValueError`` with a message written for the
model to read, which ``registry.py`` turns into a failed tool result. That is
why no handler here has a ``try`` block: the four ways a viewer call can fail
and the one way its arguments can be wrong are all handled in one place.

The camera and the pin coordinates are in model space, the same millimetres a
pin's ``point`` is recorded in, so a coordinate the model reads out of
``mesh-comments.json`` can be handed straight to ``set_view`` without any
conversion.
"""

import json

from claude_agent_sdk import tool

from . import fail, ok

#: One coordinate triple, in model space.
_POINT_SCHEMA = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}

# What a reference in ``measure`` may look like, quoted in that tool's own
# description and in the error it raises, so the two can never drift apart.
_REFERENCE_FORMS = (
    "\"pin:3\" for the human's pin 3, \"callout:2\" for your own callout 2, or "
    "\"12.5,-3.2,44\" for an explicit x,y,z point in model coordinates")


def _read_point(args, key):
    """Return ``args[key]`` as a list of three floats.

    Raises ``ValueError`` naming the field, because a model that passed two
    numbers or a string needs to be told which argument was wrong rather than
    that something was.
    """
    value = args.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s must be an array of exactly three numbers, "
                         "[x, y, z] in model coordinates" % key)
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        raise ValueError("%s must be three numbers, got %r" % (key, value))


def _read_axis(value):
    if value not in ("z", "y"):
        raise ValueError("axis must be \"z\" or \"y\", got %r" % (value,))
    return value


def build(bus):
    """Return the nine viewer tools, bound to ``bus``."""

    @tool(
        "get_view",
        "Read the 3D viewer's camera: where it is, what it is looking at, "
        "which axis is up, and the canvas size. Use it before set_view to "
        "make a relative move, or to record a view you want to return to.",
        {},
    )
    async def get_view(args):
        return ok(await bus.call("viewer.get_view"))

    @tool(
        "set_view",
        "Move the 3D viewer's camera, which the human sees happen. Give "
        "position and target as [x, y, z] in model coordinates (the same "
        "space and units as a pin's point), either or both, and optionally "
        "up_axis. To look at a pin, pass its point as the target.",
        {
            "type": "object",
            "properties": {
                "position": dict(_POINT_SCHEMA,
                                 description="where the camera goes, [x, y, z]"),
                "target": dict(_POINT_SCHEMA,
                               description="what the camera looks at and orbits, [x, y, z]"),
                "up_axis": {"type": "string", "enum": ["z", "y"],
                            "description": "which axis is up; leave out to keep the current one"},
            },
        },
    )
    async def set_view(args):
        params = {}
        if args.get("position") is not None:
            params["position"] = _read_point(args, "position")
        if args.get("target") is not None:
            params["target"] = _read_point(args, "target")
        if args.get("up_axis") is not None:
            params["up_axis"] = _read_axis(args.get("up_axis"))
        if not params:
            raise ValueError(
                "set_view needs at least one of position, target or up_axis; "
                "call get_view first if you want to move relative to where the "
                "camera is now, or fit_view to frame everything visible")
        return ok(await bus.call("viewer.set_view", params))

    @tool(
        "fit_view",
        "Frame every currently visible part in the 3D viewer, the same as the "
        "human pressing Fit. Use it after generating a part whose size or "
        "position changed a lot, or when a part has ended up off screen.",
        {},
    )
    async def fit_view(args):
        return ok(await bus.call("viewer.fit_view"))

    @tool(
        "get_visibility",
        "List the parts loaded in the 3D viewer and whether each one is shown "
        "or hidden right now, with the rel path set_visibility takes.",
        {},
    )
    async def get_visibility(args):
        return ok(await bus.call("viewer.get_visibility"))

    @tool(
        "set_visibility",
        "Show or hide one part in the 3D viewer, by the rel path list_models "
        "and get_visibility report. Use it to isolate the part being "
        "discussed, or to reveal an internal feature by hiding the shell "
        "around it.",
        {"rel": str, "visible": bool},
    )
    async def set_visibility(args):
        rel = args.get("rel")
        if not isinstance(rel, str) or not rel:
            raise ValueError("rel must be a model's rel path, as reported by "
                             "list_models or get_visibility")
        if not isinstance(args.get("visible"), bool):
            raise ValueError("visible must be true (show) or false (hide)")
        return ok(await bus.call("viewer.set_visibility",
                                 {"rel": rel, "visible": args["visible"]}))

    @tool(
        "set_up_axis",
        "Set which axis the 3D viewer treats as up, z (the default, and what "
        "most slicers expect) or y (what some CAD tools export). Refits the "
        "view. Use it when a part appears lying on its side.",
        {"axis": str},
    )
    async def set_up_axis(args):
        return ok(await bus.call("viewer.set_up_axis",
                                 {"axis": _read_axis(args.get("axis"))}))

    @tool(
        "select_pin",
        "Select one of the human's pins in the 3D viewer by its number, which "
        "highlights that pin and orbits the camera around it. Use it to show "
        "the human which of their comments you are answering.",
        {"pin": int},
    )
    async def select_pin(args):
        pin = args.get("pin")
        if not isinstance(pin, int) or isinstance(pin, bool):
            raise ValueError("pin must be a pin number, as shown in the "
                             "viewer and in mesh-comments.json")
        return ok(await bus.call("viewer.select_pin", {"pin": pin}))

    @tool(
        "capture_view",
        "Screenshot the 3D viewer as the human is currently seeing it and "
        "return it as an image. Use it to look at a part you have just "
        "generated, or to see what the human is looking at before answering a "
        "question about it. Set the camera with set_view or fit_view first if "
        "you need a particular angle.",
        {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 64, "maximum": 1568,
                          "description": "pixel width to render at; leave out for "
                                         "the canvas's own size"},
            },
        },
    )
    async def capture_view(args):
        params = {}
        width = args.get("width")
        if width is not None:
            if not isinstance(width, int) or isinstance(width, bool):
                raise ValueError("width must be an integer number of pixels")
            params["width"] = width
        result = await bus.call("viewer.capture_view", params)
        image = (result or {}).get("image") or ""
        prefix, _, data = image.partition(",")
        if not data or not prefix.startswith("data:image/") or not prefix.endswith(";base64"):
            # The browser owns the encoding, so this is a bug on that side
            # rather than anything the model did. Said plainly, because the
            # alternative is a model that received an image block containing
            # nothing and has no way to tell that from a blank render.
            return fail("the viewer returned a capture this build could not read; "
                        "the image was not delivered")
        media_type = prefix[len("data:"):-len(";base64")]
        # Fact 8: an image item in a tool result reaches the model as a real
        # image, so the capture needs no file on disk and no second round trip.
        # The text block alongside it carries the numbers an image cannot: what
        # the camera was, and what size the capture actually is.
        facts = json.dumps({
            "width": result.get("width"),
            "height": result.get("height"),
            "format": result.get("format"),
            "camera": result.get("camera"),
        }, indent=2, default=str)
        return {"content": [
            {"type": "image", "data": data, "mimeType": media_type},
            {"type": "text", "text": facts},
        ]}

    @tool(
        "measure",
        "Measure the straight-line distance between two points in the 3D "
        "viewer, returning the per-axis deltas and the distance in model "
        "units. Each of a and b is " + _REFERENCE_FORMS + ". Pins the human "
        "has placed but not yet submitted are measurable too, since this "
        "reads the live viewer rather than the comments file.",
        {"a": str, "b": str},
    )
    async def measure(args):
        for key in ("a", "b"):
            if not isinstance(args.get(key), str) or not args[key].strip():
                raise ValueError("%s must be %s" % (key, _REFERENCE_FORMS))
        return ok(await bus.call("viewer.measure",
                                 {"a": args["a"].strip(), "b": args["b"].strip()}))

    return [get_view, set_view, fit_view, get_visibility, set_visibility,
            set_up_axis, select_pin, capture_view, measure]
