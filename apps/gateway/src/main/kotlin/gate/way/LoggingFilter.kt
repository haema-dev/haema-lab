package gate.way

import org.slf4j.LoggerFactory
import org.springframework.cloud.gateway.filter.GlobalFilter
import org.springframework.cloud.gateway.support.ServerWebExchangeUtils.GATEWAY_ROUTE_ATTR
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import reactor.core.publisher.Mono

@Configuration
class LoggingFilter {
    private val log = LoggerFactory.getLogger(javaClass)

    @Bean
    fun customGlobalFilter(): GlobalFilter {
        return GlobalFilter { exchange, chain ->
            val request = exchange.request
            // 1. 요청 유입 기록 (Inbound Request)
            log.info(">>>> [REQUEST] ID: ${request.id} | IP: ${request.remoteAddress} | PATH: ${request.uri}")

            chain.filter(exchange).then(Mono.fromRunnable {
                // 2. 라우팅 정보 추출
                val route = exchange.getAttribute<org.springframework.cloud.gateway.route.Route>(GATEWAY_ROUTE_ATTR)
                val forwardUri = route?.uri ?: "UNKNOWN"

                // 3. 응답 송출 기록 (Outbound Response)
                log.info("<<<< [RESPONSE] ID: ${request.id} | ROUTE: ${route?.id} | TO: $forwardUri | STATUS: ${exchange.response.statusCode}")
            })
        }
    }
}