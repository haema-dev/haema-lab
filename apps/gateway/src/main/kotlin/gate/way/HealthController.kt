package gate.way

import org.springframework.http.HttpStatus
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController
import org.springframework.http.ResponseEntity
import java.net.URI

@RestController
class HealthController {
    // 게이트웨이 라우팅 엔진을 타기 전에 Spring WebFlux 레벨에서 직접 응답한다.
    @GetMapping("/ping")
    fun ping(): ResponseEntity<String> {
        return ResponseEntity.ok("OK") // 즉시 200 OK와 함께 "OK" 반환
    }
}