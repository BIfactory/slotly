from __future__ import annotations

"""Base view class for all MCP API views.

All MCP views (T3-T6) should inherit from MCPAPIView to get:
  - The MCP exception handler (custom error shapes).

Authentication, permissions, and throttling are set per-view or on sub-bases.
This class is intentionally minimal — no business logic here.
"""

from rest_framework.views import APIView

from .auth import MCPTokenAuthentication
from .exceptions import mcp_exception_handler


class MCPAPIView(APIView):
    """APIView that routes exceptions through the MCP handler.

    Sets MCPTokenAuthentication as the default so that any future subclass
    that forgets to declare authentication_classes still uses Bearer auth
    rather than DRF's default SessionAuthentication.  Current subclasses
    (ToolsListView, ToolsCallView) keep their own explicit declarations —
    that is fine; they shadow this value with the same list.
    """

    authentication_classes = [MCPTokenAuthentication]

    def get_exception_handler(self):
        return mcp_exception_handler
