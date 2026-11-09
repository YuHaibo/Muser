#pragma once
#include <cstdint>
#include <functional>
#include <map>
#include <string>
enum { K_ESC=256,K_ENTER,K_BACKSPACE,K_TAB,K_UP,K_DOWN,K_LEFT,K_RIGHT,K_DELETE };
struct KeyEvent { int key; bool down; std::string text; bool repeat=false; };
class Input {
    [[maybe_unused]] int fd_=-1;
    [[maybe_unused]] bool shift_=false,alt_=false;
    std::map<int,std::pair<std::string,std::string>> keymap_;
public:
    Input(); ~Input();
    void poll(const std::function<void(KeyEvent)>& callback);
};
