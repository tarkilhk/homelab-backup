## Multi-stage build for Vite React app served by Nginx

# 1) Build stage
FROM node:22-alpine AS builder
WORKDIR /app

# Install dependencies first (better layer caching)
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Copy the rest and build
COPY . .
RUN npm run build

# 2) Runtime stage
FROM nginx:1.27-alpine AS runner

# Copy Nginx config template and entrypoint for envsubst
COPY nginx.conf /etc/nginx/conf.d/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Install envsubst
RUN apk add --no-cache gettext

# Copy build output
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]


