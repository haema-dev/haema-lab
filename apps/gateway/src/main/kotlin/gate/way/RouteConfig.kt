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
            // 1. 상태 점검용
            route("health_check") {
                path("/ping")
                filters {
                    // 즉시 200 상태를 하달하라!
                    setStatus(200)
                }
                // 내부 리다이렉트를 하지 말고, 아무 일도 하지 않는 가짜 URI를 줘라.
                // forward 대신 실제 존재하지 않는 빈 주소를 주는 것이 루프를 막는 방법이다.
                uri("no-op://dummmy")
            }

            // 2. ArgoCD
//            route("argocd_route") {
//                order(-1)
//                path("/admin/argocd", "/admin/argocd/", "/admin/argocd/**")
//                filters {
//                    //  /admin/argocd 2마디 자르기
//                    stripPrefix(2)
//                }
//                uri("http://argocd-server.argocd.svc.cluster.local:80")
//            }

            // 3. 프론트엔드 (나머지 모든 경로 /**)
            route("frontend_route") {
                path("/**")
                uri("http://cloud-frontend-svc.default.svc.cluster.local")
            }

            // 4.백엔드 API (가장 구체적인 경로를 우선 배치)
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