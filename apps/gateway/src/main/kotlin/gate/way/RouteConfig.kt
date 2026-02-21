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
            // 1. 프론트엔드 (나머지 모든 경로 /**)
            route("frontend_route") {
                path("/**")
                uri("http://cloud-frontend-svc.default.svc.cluster.local")
            }

            // 2. 상태 점검용 (문제의 구간)
            route("health_check") {
                path("/ping")
                filters { setStatus(200) }
                // 'no-need-url' 대신 유효한 URI를 사용하라!
                // 필터에서 200을 뱉으므로 실제 연결은 되지 않으나 형식은 지켜야 한다.
                uri("forward:///health-check-dummy")
            }
        }
    }
}