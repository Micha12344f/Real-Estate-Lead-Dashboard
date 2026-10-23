module.exports = function handler(_req, res) {
  var bookingAppBaseUrl = String(process.env.BOOKING_APP_BASE_URL || "").trim().replace(/\/+$/, "");
  var questionnaireSubmitBaseUrl = String(process.env.QUESTIONNAIRE_SUBMIT_BASE_URL || "").trim().replace(/\/+$/, "");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.setHeader("Access-Control-Allow-Origin", "*");
  var config = {};
  if (bookingAppBaseUrl) config.bookingAppBaseUrl = bookingAppBaseUrl;
  if (questionnaireSubmitBaseUrl) config.questionnaireSubmitBaseUrl = questionnaireSubmitBaseUrl;
  res.status(200).json(config);
};
