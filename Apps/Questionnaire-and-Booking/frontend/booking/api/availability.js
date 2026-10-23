// Vercel serverless proxy: GET /api/availability?token=...
// Proxies to the configured backend webhook (server-to-server).
module.exports = async function handler(req, res) {
  // CORS headers for the frontend
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();
  if (req.method !== "GET") return res.status(405).json({ status: "error", message: "Method not allowed." });

  try {
    const token = (req.query.token || "").trim();
    if (!token) return res.status(400).json({ status: "invalid_token", message: "Missing token parameter." });

    const baseUrl = String(process.env.BOOKING_BASE_URL || "").trim().replace(/\/+$/, "");
    const apiKey = String(process.env.X_TVE_API_KEY || "").trim();
    if (!baseUrl || !apiKey) return res.status(503).json({ status: "configuration_error", message: "Booking backend URL and webhook API key must be configured on the server." });
    const params = new URLSearchParams({ token });
    for (const key of ["propertyId", "contactId"]) {
      if (req.query[key] !== undefined) params.set(key, String(req.query[key]));
    }
    const url = `${baseUrl}/thames-valley-estates/booking-availability?${params}`;

    const response = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json", "X-TVE-Auth": apiKey }
    });

    const body = await response.text();
    let data;
    try { data = JSON.parse(body); } catch { data = null; }

    res.status(response.status).json(data || { status: "error", message: body });
  } catch (err) {
    res.status(502).json({ status: "error", message: err.message || "Availability proxy failed." });
  }
};
