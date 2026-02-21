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

            // 3.백엔드 API (가장 구체적인 경로를 우선 배치)
//            route("backend_service") {
//                path("/api/v1/**")
//                filters {
//                    stripPrefix(1) // /api/v1/user -> /user 로 변경
//                    addRequestHeader("X-Gateway-Auth", "Authorized-By-Kotlin")
//                }
//                uri("http://cloud-backend-svc.default.svc.cluster.local")
//            }
        }
    }
}