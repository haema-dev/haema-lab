package gate.way

import org.springframework.cloud.gateway.route.RouteLocator
import org.springframework.cloud.gateway.route.builder.RouteLocatorBuilder
import org.springframework.cloud.gateway.route.builder.filters
import org.springframework.cloud.gateway.route.builder.routes
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

@Configuration
class RouteConfig {

    @Bean
    fun customRouteLocator(builder: RouteLocatorBuilder): RouteLocator {
        return builder.routes {
            // 1. 테스트용: 외부 httpbin 서비스로 포워딩
            route("test_route") {
                path("/get/**")
                uri("https://httpbin.org")
            }

            route("external_test") {
                path("/google/**")
                filters { stripPrefix(1) }
                uri("https://www.google.com/robots.txt") // 내 로컬 PC에서 구글로 잘 넘겨주는지 확인
            }

            // 2. 실전: K8s 내부의 Django 서비스로 라우팅
            // 게이트웨이가 K8s 내부로 들어왔으므로 서비스 이름(DNS)으로 바로 호출 가능합니다.
            route("django_service") {
                path("/api/v1/**")
                filters {
                    addRequestHeader("X-Gateway-Auth", "Authorized-By-Kotlin")
                    stripPrefix(1) // /api/v1/user -> /user 로 변경하여 전달
                }
                uri("http://django-svc.default.svc.cluster.local:8000")
            }

            // 3. 고정 응답 (Health Check 대용)
            route("health_check") {
                path("/ping")
                filters {
                    setStatus(200)
                    // 필요하다면 가벼운 응답 본문을 추가할 수도 있습니다.
                }
                // 'no-need-url' 대신 유효한 더미 URL을 넣으세요.
                // 'forward:///' 스키마를 쓰거나 임의의 http 주소를 넣으면 됩니다.
                uri("forward:///static-response")
            }
        }
    }
}