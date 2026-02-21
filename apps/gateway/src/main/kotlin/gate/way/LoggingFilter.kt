package gate.way

import org.slf4j.LoggerFactory
import org.springframework.cloud.gateway.filter.GlobalFilter
import org.springframework.cloud.gateway.support.ServerWebExchangeUtils.GATEWAY_ROUTE_ATTR
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.core.Ordered
import reactor.core.publisher.Mono

@Configuration
class LoggingFilter {
    private val log = LoggerFactory.getLogger(javaClass)

    @Bean
    fun customGlobalFilter(): GlobalFilter {
        return GlobalFilter { exchange, chain ->
            val request = exchange.request
            // 1. 요청 도착 보고
            log.info(">>>> [입입] 요청 도착: ${request.id} | 출처: ${request.remoteAddress} | 경로: ${request.uri}")

            chain.filter(exchange).then(Mono.fromRunnable {
                // 2. 라우팅 결과 확인
                val route = exchange.getAttribute<org.springframework.cloud.gateway.route.Route>(GATEWAY_ROUTE_ATTR)
                val forwardUri = route?.uri ?: "UNKNOWN"

                // 3. 최종 목적지 및 상태 보고
                log.info("<<<< [출격] 요청 처리: ${request.id} | 선택된 루트: ${route?.id} | 최종 목적지: $forwardUri | 상태코드: ${exchange.response.statusCode}")
            })
        }
    }
}