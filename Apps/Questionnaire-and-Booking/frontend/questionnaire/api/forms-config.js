// Public, read-only configuration. The backend credential stays on the server.
module.exports = async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store, max-age=0');
  if (req.method !== 'GET') return res.status(405).json({ message: 'Method not allowed.' });
  const base = process.env.FORMS_CONFIG_URL || process.env.QUESTIONNAIRE_SUBMIT_BASE_URL || process.env.BOOKING_BASE_URL || process.env.BOOKING_PRIMARY_URL;
  const key = process.env.X_TVE_API_KEY;
  if (!base || !key) return res.status(503).json({ message: 'Form settings are not configured.' });
  try {
    // Explicit URL supports installations that mount the backend under a prefix.
    const target = process.env.FORMS_CONFIG_URL || new URL('/webhook/thames-valley-estates/forms-config', base).href;
    const response = await fetch(target, { headers: { 'X-TVE-Auth': key, Accept: 'application/json' }, signal: AbortSignal.timeout(8000) });
    if (!response.ok) return res.status(503).json({ message: 'Form settings are temporarily unavailable.' });
    const data = await response.json();
    return res.status(200).json({ logo_url: data.logo_url, questions: data.questions });
  } catch (_) {
    return res.status(502).json({ message: 'Form settings could not be loaded.' });
  }
};
