ARG NODE_BASE_IMAGE=node:22-alpine
FROM ${NODE_BASE_IMAGE}
WORKDIR /app
COPY serve-local.js ./
COPY booking/index.html ./booking/index.html
COPY booking/country-codes.js ./booking/country-codes.js
COPY booking/assets ./booking/assets
COPY questionnaire/index.html ./questionnaire/index.html
COPY questionnaire/assets ./questionnaire/assets
COPY dashboard ./dashboard
CMD ["node", "serve-local.js"]
