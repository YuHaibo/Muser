#include "bridge.h"
#include <chrono>
#include <csignal>
#include <fcntl.h>
#include <stdexcept>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

Bridge::Bridge(const std::string& root, bool demo) {
    int to[2], from[2];
    if (pipe(to) || pipe(from)) throw std::runtime_error("Cannot create backend pipes");
    for (int fd : {to[0],to[1],from[0],from[1]}) fcntl(fd,F_SETFD,FD_CLOEXEC);
    pid_ = fork();
    if (pid_ < 0) throw std::runtime_error("Cannot start backend");
    if (pid_ == 0) {
        setpgid(0,0);
        dup2(to[0],STDIN_FILENO); dup2(from[1],STDOUT_FILENO);
        for (int fd : {to[0],to[1],from[0],from[1]}) close(fd);
        const char* python = std::getenv("MUSER_PYTHON");
        if (!python) python = "python3";
        std::string script = root + "/backend/worker.py";
        if (demo) execlp(python,python,"-u",script.c_str(),"--demo",nullptr);
        else execlp(python,python,"-u",script.c_str(),nullptr);
        _exit(127);
    }
    close(to[0]); close(from[1]); input_=to[1]; output_=from[0];
    fcntl(input_,F_SETFL,fcntl(input_,F_GETFL)|O_NONBLOCK);
    fcntl(output_,F_SETFL,fcntl(output_,F_GETFL)|O_NONBLOCK);
    signal(SIGPIPE,SIG_IGN);
}
void Bridge::send(const json& value) {
    if (outgoing_.size() < 65536) outgoing_ += value.dump() + "\n";
}
std::vector<json> Bridge::poll() {
    std::vector<json> result;
    if (!outgoing_.empty()) {
        auto n=write(input_,outgoing_.data(),outgoing_.size());
        if(n>0) outgoing_.erase(0,n);
    }
    char buf[8192]; ssize_t n;
    while((n=read(output_,buf,sizeof(buf)))>0) {
        incoming_.append(buf,n);
        if(incoming_.size()>262144) {incoming_.clear(); failed_=true; break;}
        size_t end;
        while((end=incoming_.find('\n'))!=std::string::npos) {
            auto item=json::parse(incoming_.substr(0,end),nullptr,false);
            incoming_.erase(0,end+1);
            if(item.is_object()) result.push_back(std::move(item));
        }
    }
    int status;
    if(pid_>0 && waitpid(pid_,&status,WNOHANG)==pid_) {pid_=-1; failed_=true;}
    if(failed_) {
        result.push_back({{"type","error"},{"code","backend_closed"},{"message","后端已停止，请退出后重新打开"}});
        failed_=false;
    }
    return result;
}
Bridge::~Bridge() {
    if(pid_>0) {
        send({{"command","shutdown"}});
        for(int i=0;i<180;i++) {
            poll(); if(pid_<0) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            if(i==150) kill(-pid_,SIGTERM);
        }
        if(pid_>0) {kill(-pid_,SIGKILL); waitpid(pid_,nullptr,0);}
    }
    if(input_>=0)close(input_);
    if(output_>=0)close(output_);
}
