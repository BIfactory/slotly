import type { NextConfig } from "next";

// Backend Django origin. Override via NEXT_PUBLIC_API_BASE_URL in .env if needed.
const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  /**
   * Reverse-proxy Django endpoints under the same origin as the Next.js app.
   * This makes Django's session + CSRF cookies same-origin from the browser's
   * perspective, sidestepping `SameSite=Lax` cross-origin restrictions in dev.
   */
  async rewrites() {
    return [
      { source: "/_allauth/:path*", destination: `${API}/_allauth/:path*` },
      { source: "/api/:path*", destination: `${API}/api/:path*` },
      { source: "/healthz", destination: `${API}/healthz` },
      // MCP spec endpoint — bearer-token-authed, external agents (Personal
      // Hub etc.) call `/mcp/v1/tools/{list,call}`. Must be forwarded to
      // Django just like /api/*, otherwise requests fall through to Next
      // which has no matching route and returns a 404 HTML page.
      { source: "/mcp/v1/:path*", destination: `${API}/mcp/v1/:path*` },
    ];
  },
  /**
   * /settings/teams was the old URL for what is now /groups (M21 rename).
   * Permanent redirect so old bookmarks and any cached links still land
   * the user on the right page.
   */
  async redirects() {
    return [
      { source: "/settings/teams", destination: "/groups", permanent: true },
      { source: "/settings/teams/:id", destination: "/groups/:id", permanent: true },
      { source: "/connections", destination: "/people", permanent: true },
      // Integrations merged into Calendars (both are "connect a calendar";
      // splitting them across two settings screens was confusing users).
      // Preserve any query string (e.g. ?google=connected after OAuth callback).
      { source: "/settings/integrations", destination: "/settings/calendars", permanent: true },
    ];
  },
};

export default nextConfig;
