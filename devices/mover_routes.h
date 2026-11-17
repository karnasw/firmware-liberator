#pragma once
// The movers' two routes (devices/esp32c3/mover.yaml, devices/esp32/mover.yaml):
//
// - GET /mover answers its project's name and version, so the conversion tool
//   knows the mover even while its update service is busy or stuck - the one
//   thing it otherwise goes by.
// - POST /restart answers 200, then the mover restarts - as switching the
//   device off and on would, so the tool need not ask anyone to. A mover
//   restarted starts again: nothing on the device changes.
//
// They are on the small web server the captive portal brings in, which
// otherwise runs only while the mover's own network is up: started here, it
// answers on the home network too. Its init() counts its users, so the portal
// stopping leaves it running.
#include "esphome/core/application.h"
#include "esphome/core/defines.h"
#include "esphome/components/web_server_base/web_server_base.h"

namespace autoflash {

class MoverRoutes : public esphome::web_server_idf::AsyncWebHandler {
 public:
  bool canHandle(esphome::web_server_idf::AsyncWebServerRequest *request) const override {
    char url[esphome::web_server_idf::AsyncWebServerRequest::URL_BUF_SIZE];
    auto path = request->url_to(url);
    return (request->method() == HTTP_GET && path == "/mover") ||
           (request->method() == HTTP_POST && path == "/restart");
  }
  void handleRequest(esphome::web_server_idf::AsyncWebServerRequest *request) override {
    if (request->method() == HTTP_GET) {
      request->send(200, "text/plain", ESPHOME_PROJECT_NAME " " ESPHOME_PROJECT_VERSION);
      return;
    }
    request->send(200, "text/plain", "Restarting");
    // The request runs in the web server's task: the restart comes from the
    // main loop, a moment after the answer has gone.
    esphome::App.scheduler.set_timeout(this, 200, []() { esphome::App.safe_reboot(); });
  }
};

inline void start_routes() {
  auto *base = esphome::web_server_base::global_web_server_base;
  base->add_handler_without_auth(new MoverRoutes());
  base->init();
}

}  // namespace autoflash
