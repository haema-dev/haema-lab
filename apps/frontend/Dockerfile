# 1단계: 빌드 환경 (Build Stage)
FROM node:22-alpine AS build
WORKDIR /app

# pnpm 설치 (동무가 워크플로우에서 pnpm을 썼으므로)
RUN npm install -g pnpm

# 의존성 설치
COPY pnpm-lock.yaml ./
COPY package.json ./
RUN pnpm install

# 소스 복사 및 빌드
COPY . .
RUN pnpm run build

# 2단계: 실행 환경 (Production Stage)
FROM nginx:stable-alpine
# 빌드된 정적 파일들을 Nginx로 복사
COPY --from=build /app/dist /usr/share/nginx/html

# Nginx 기본 포트 개방
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]