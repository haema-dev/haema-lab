# 1. 실행 환경 설정 (JRE 21 사용)
FROM eclipse-temurin:21-jre-alpine

# 2. 작업 디렉토리 생성
WORKDIR /app

# 3. 빌드된 JAR 파일 복사
# deploy-gateway.yml에서 빌드한 결과물을 가져옵니다.
COPY build/libs/*.jar app.jar

# 4. 포트 설정 (Gateway 기본 포트)
EXPOSE 8080

# 5. 애플리케이션 실행
ENTRYPOINT ["java", "-jar", "app.jar"]