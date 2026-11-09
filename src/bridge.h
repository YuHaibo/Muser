#pragma once
#include "json.hpp"
#include <string>
#include <vector>
using json = nlohmann::json;
class Bridge {
    int pid_ = -1, input_ = -1, output_ = -1;
    std::string incoming_, outgoing_;
    bool failed_ = false;
public:
    Bridge(const std::string& root, bool demo);
    ~Bridge();
    void send(const json& value);
    std::vector<json> poll();
};
