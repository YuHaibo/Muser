#include "input.h"
#include <fstream>
#include <sstream>
#include <stdexcept>
#if !USE_DESKTOP
#include <fcntl.h>
#include <linux/input.h>
#include <unistd.h>
#include <cstdlib>
#endif
Input::Input() {
#if !USE_DESKTOP
    auto env=getenv("MUSER_INPUT_DEVICE");
    fd_=open(env?env:"/dev/input/by-path/platform-3f804000.i2c-event",O_RDONLY|O_NONBLOCK|O_CLOEXEC);
    if(fd_<0) throw std::runtime_error("Cannot open keyboard; set MUSER_INPUT_DEVICE");
    std::ifstream map("/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map");
    std::string line;
    while(std::getline(map,line)) {
        std::istringstream s(line); std::string tag,eq,a,b; int code;
        if(s>>tag>>code>>eq>>a && tag=="keycode" && eq=="=") {
            if(!(s>>b)||b[0]=='#') b=a;
            keymap_[code]={a,b};
        }
    }
#endif
}
Input::~Input() {
#if !USE_DESKTOP
    if(fd_>=0)close(fd_);
#endif
}
void Input::poll(const std::function<void(KeyEvent)>& callback) {
#if !USE_DESKTOP
    static const std::map<std::string,std::string> symbols={
        {"space"," "},{"exclam","!"},{"at","@"},{"numbersign","#"},{"dollar","$"},
        {"percent","%"},{"asciicircum","^"},{"ampersand","&"},{"asterisk","*"},
        {"parenleft","("},{"parenright",")"},{"minus","-"},{"underscore","_"},
        {"equal","="},{"plus","+"},{"bracketleft","["},{"bracketright","]"},
        {"braceleft","{"},{"braceright","}"},{"semicolon",";"},{"colon",":"},
        {"apostrophe","'"},{"quotedbl","\""},{"grave","`"},{"asciitilde","~"},
        {"backslash","\\"},{"bar","|"},{"comma",","},{"less","<"},{"period","."},
        {"greater",">"},{"slash","/"},{"question","?"}};
    static const std::map<std::string,int> special={{"Escape",K_ESC},{"Return",K_ENTER},
        {"BackSpace",K_BACKSPACE},{"Tab",K_TAB},{"Up",K_UP},{"Down",K_DOWN},{"Left",K_LEFT},{"Right",K_RIGHT},{"Delete",K_DELETE}};
    input_event e;
    while(read(fd_,&e,sizeof(e))==sizeof(e)) {
        if(e.type!=EV_KEY)continue;
        if(e.code==KEY_LEFTSHIFT||e.code==KEY_RIGHTSHIFT){shift_=e.value;continue;}
        if(e.code==KEY_RIGHTALT||e.code==KEY_LEFTALT){alt_=e.value;continue;}
        int key=0; std::string text;
        if(keymap_.count(e.code)) {
            auto name=shift_?keymap_[e.code].second:keymap_[e.code].first;
            if(name[0]=='+') name.erase(0,1);
            if(special.count(name))key=special.at(name);
            else if(symbols.count(name))text=symbols.at(name);
            else if(name.size()==1)text=name;
        }
        if(!key && text.empty()) {
            switch(e.code) {
                case KEY_ESC:key=K_ESC;break; case KEY_ENTER:key=K_ENTER;break;
                case KEY_BACKSPACE:key=K_BACKSPACE;break; case KEY_TAB:key=K_TAB;break;
                case KEY_UP:key=K_UP;break;case KEY_DOWN:key=K_DOWN;break;
                case KEY_LEFT:key=K_LEFT;break;case KEY_RIGHT:key=K_RIGHT;break;
                case KEY_SPACE:text=" ";break;default:break;
            }
            static const std::map<int,char> letters={{KEY_A,'a'},{KEY_B,'b'},{KEY_C,'c'},{KEY_D,'d'},
                {KEY_E,'e'},{KEY_F,'f'},{KEY_G,'g'},{KEY_H,'h'},{KEY_I,'i'},{KEY_J,'j'},{KEY_K,'k'},
                {KEY_L,'l'},{KEY_M,'m'},{KEY_N,'n'},{KEY_O,'o'},{KEY_P,'p'},{KEY_Q,'q'},{KEY_R,'r'},
                {KEY_S,'s'},{KEY_T,'t'},{KEY_U,'u'},{KEY_V,'v'},{KEY_W,'w'},{KEY_X,'x'},{KEY_Y,'y'},{KEY_Z,'z'}};
            if(letters.count(e.code))text=std::string(1,letters.at(e.code)-(shift_?32:0));
            if(e.code>=KEY_1&&e.code<=KEY_0) {
                std::string normal="1234567890", shifted="!@#$%^&*()";
                text=std::string(1,(shift_?shifted:normal)[e.code-KEY_1]);
            }
        }
        if(!key && text.size()==1)key=text[0];
        if(key)callback({key,e.value!=0,text,e.value==2});
    }
#else
    (void)callback;
#endif
}
