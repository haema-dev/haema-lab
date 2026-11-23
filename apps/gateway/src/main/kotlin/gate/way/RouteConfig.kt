package gate.way

import org.springframework.beans.factory.annotation.Value
import org.springframework.cloud.gateway.route.RouteLocator
import org.springframework.cloud.gateway.route.builder.RouteLocatorBuilder
import org.springframework.cloud.gateway.route.builder.filters
import org.springframework.cloud.gateway.route.builder.routes
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

@Configuration
class RouteConfig(
    // 기본 꺼짐. Gateway 는 Cloudflare Tunnel 로 인터넷에 열려 있고 로그인이 아직 없어서,
    // 켜면 누구나 테스트 유저 1 로 API 를 쓴다. 접근 제한(Cloudflare Access 등)을 건 뒤에만 켠다.
    // 켜는 법: gateway-deployment 에 env GATEWAY_BACKENDAPI_ENABLED=true
    @Value("\${gateway.backend-api.enabled:false}") private val backendApiEnabled: Boolean,
) {

    @Bean
    fun customRouteLocator(builder: RouteLocatorBuilder): RouteLocator {
        return builder.routes {
            // 1. 백엔드 API. /** 보다 먼저 매칭되도록 order 를 낮춘다. 경로는 그대로 넘긴다 (Django 가 /api/chat/... 를 받음).
            //    Django 는 X-User-Id 를 그대로 믿으므로 클라이언트가 보낸 값은 덮어쓴다.
            //    로그인이 없어 지금은 모든 요청을 테스트 유저 1 로 넣는다. 인증을 붙이면 이 줄을 인증 결과로 바꾼다.
            if (backendApiEnabled) {
                route(id = "backend_api", order = -1) {
                    path("/api/**")
                    filters {
                        setRequestHeader("X-User-Id", "1")
                    }
                    uri("http://cloud-backend-svc.default.svc.cluster.local")
                }
            }

            // 2. 프론트엔드 (나머지 모든 경로 /**)
            route("frontend_route") {
                path("/**")
                uri("http://cloud-frontend-svc.default.svc.cluster.local")
            }
        }
    }
}