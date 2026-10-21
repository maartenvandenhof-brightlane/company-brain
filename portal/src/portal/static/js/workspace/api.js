// Fetching the workspace's JSON (/wiki/api/*): same origin, the session cookie, errors as
// exceptions with the server's message. A lost session sends the whole page to sign in.
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function getJSON(url, { background = false, signal } = {}) {
  const headers = { Accept: "application/json" };
  // automatic refreshes must not keep an idle session alive (see portal.wiki)
  if (background) headers["X-Portal-Background"] = "1";
  let response;
  try {
    response = await fetch(url, { credentials: "same-origin", headers, signal });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("The portal is not reachable. Check your connection.", 0);
  }
  if (response.status === 401 && !background) {
    const here = window.location.pathname + window.location.search;
    window.location.assign(`/login?next=${encodeURIComponent(here)}`);
  }
  let body = {};
  try {
    body = await response.json();
  } catch {
    // an HTML error page: keep the status
  }
  if (!response.ok) throw new ApiError(body.error || `The portal answered ${response.status}.`, response.status);
  return body;
}
