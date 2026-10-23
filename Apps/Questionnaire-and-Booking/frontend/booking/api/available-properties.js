// Vercel serverless proxy: GET/POST /api/available-properties (upstream read-only GET).
// Proxies to the configured backend webhook (server-to-server).
module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();
  if (!["GET", "POST"].includes(req.method)) return res.status(405).json({ status: "error", message: "Method not allowed." });

  try {
    const baseUrl = String(process.env.BOOKING_BASE_URL || "").trim().replace(/\/+$/, "");
    const apiKey = String(process.env.X_TVE_API_KEY || "").trim();
    if (!baseUrl || !apiKey) return res.status(503).json({ status: "configuration_error", message: "Booking backend URL and webhook API key must be configured on the server." });
    const url = `${baseUrl}/thames-valley-estates/booking-available-properties`;

    const response = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json", "X-TVE-Auth": apiKey }
    });

    const responseText = await response.text();
    let data;
    try { data = JSON.parse(responseText); } catch { data = null; }

    res.status(response.status).json(data || { status: "error", message: responseText });
  } catch (err) {
    res.status(502).json({ status: "error", message: err.message || "Available properties proxy failed." });
  }
};
