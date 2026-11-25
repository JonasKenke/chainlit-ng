from chainlit.utils import check_module_version

if not check_module_version("langflow", "0.1.4"):
    raise ValueError(
        "Expected Langflow version >= 0.1.4. Run `pip install langflow --upgrade`"
    )

import json
from typing import Dict, Optional, Union

from chainlit.url_fetch import fetch_url_bytes


async def load_flow(schema: Union[Dict, str], tweaks: Optional[Dict] = None):
    from langflow import load_flow_from_json

    if isinstance(schema, str):
        schema = json.loads(await fetch_url_bytes(schema))

    flow = load_flow_from_json(flow=schema, tweaks=tweaks)

    return flow
