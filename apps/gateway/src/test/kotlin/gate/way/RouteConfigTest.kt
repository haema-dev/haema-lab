package gate.way

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.cloud.gateway.filter.GatewayFilterChain
import org.springframework.cloud.gateway.route.Route
import org.springframework.cloud.gateway.route.RouteLocator
import org.springframework.mock.http.server.reactive.MockServerHttpRequest
import org.springframework.mock.web.server.MockServerWebExchange
import org.springframework.web.server.ServerWebExchange
import reactor.core.publisher.Mono

// 실제 백엔드·프론트로 요청을 보내지 않는다. 어떤 라우트가 고르는지와 필터가 바꾼 헤더만 본다.

private fun RouteLocator.firstMatch(exchange: ServerWebExchange): Route =
	routes.filterWhen { Mono.from(it.predicate.apply(exchange)) }.blockFirst()!!

private fun applyFilters(route: Route, exchange: ServerWebExchange): ServerWebExchange {
	var result = exchange
	for (filter in route.filters.reversed()) {
		val next = result
		filter.filter(next, GatewayFilterChain { ex -> result = ex; Mono.empty() }).block()
	}
	return result
}

private fun get(path: String, vararg headers: Pair<String, String>): ServerWebExchange {
	val request = MockServerHttpRequest.get(path)
	headers.forEach { (name, value) -> request.header(name, value) }
	return MockServerWebExchange.from(request)
}

@SpringBootTest
class RouteConfigDefaultTest {

	@Autowired
	lateinit var routeLocator: RouteLocator

	@Test
	fun `기본값에서는 api 경로가 꺼져 있어 백엔드로 가지 않는다`() {
		assertEquals("frontend_route", routeLocator.firstMatch(get("/api/chat/conversations")).id)
	}
}

@SpringBootTest(properties = ["gateway.backend-api.enabled=true"])
class RouteConfigBackendEnabledTest {

	@Autowired
	lateinit var routeLocator: RouteLocator

	@Test
	fun `api 요청은 백엔드로 가고 X-User-Id 는 클라이언트 값과 무관하게 1 이다`() {
		val exchange = get("/api/chat/conversations", "X-User-Id" to "999")
		val route = routeLocator.firstMatch(exchange)

		assertEquals("backend_api", route.id)
		assertEquals("cloud-backend-svc.default.svc.cluster.local", route.uri.host)
		assertEquals(listOf("1"), applyFilters(route, exchange).request.headers["X-User-Id"])
	}

	@Test
	fun `api 가 아닌 요청은 프론트로 간다`() {
		assertEquals("frontend_route", routeLocator.firstMatch(get("/")).id)
	}
}
