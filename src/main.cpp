// Muser's LVGL shell; platform structure follows CardputerZero/Template (MIT).
#include "lvgl.h"
#include "bridge.h"
#include "input.h"
extern "C" {
#include "muse_pixel.h"
}
#include <png.h>
#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#if USE_DESKTOP
#include <SDL.h>
#else
#include "src/drivers/display/fb/lv_linux_fbdev.h"
#endif
namespace fs=std::filesystem;
static volatile sig_atomic_t interrupted=0;
static void stop_signal(int){interrupted=1;}
static uint32_t ticks(){
    static auto start=std::chrono::steady_clock::now();
    return uint32_t(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now()-start).count());
}
static std::string root_path(){
    if(auto e=std::getenv("MUSER_ROOT"))return e;
    if(fs::exists("/usr/share/muser/backend/worker.py"))return "/usr/share/muser";
    return MUSER_SOURCE_ROOT;
}
static lv_color_t color(unsigned value){return lv_color_hex(value);}
static constexpr unsigned BG=0x0c1015,TEXT=0xe8eaf0,DIM=0x85909d,ACCENT=0x4cabff;
static lv_obj_t* plain(lv_obj_t* parent,int x,int y,int w,int h,unsigned bg=BG){
    auto o=lv_obj_create(parent);lv_obj_remove_style_all(o);lv_obj_set_pos(o,x,y);lv_obj_set_size(o,w,h);
    lv_obj_set_style_bg_color(o,color(bg),0);lv_obj_set_style_bg_opa(o,LV_OPA_COVER,0);
    lv_obj_remove_flag(o,LV_OBJ_FLAG_SCROLLABLE);return o;
}
static lv_obj_t* label(lv_obj_t* parent,const lv_font_t* font,int x,int y,int w,const char* text,unsigned c=TEXT){
    auto o=lv_label_create(parent);lv_obj_set_pos(o,x,y);lv_obj_set_width(o,w);
    lv_obj_set_style_text_font(o,font,0);lv_obj_set_style_text_color(o,color(c),0);
    lv_obj_set_style_text_line_space(o,2,0);lv_label_set_text(o,text);return o;
}
struct Message{std::string id,role,text;bool final;};
class UI{
    Bridge& bridge_;
    const lv_font_t *font_,*cjk_;
    lv_obj_t *root_,*body_=nullptr,*header_=nullptr,*footer_=nullptr,*avatar_=nullptr,
        *caption_=nullptr,*role_=nullptr,*scroll_=nullptr,*editor_=nullptr,*settings_rows_=nullptr,*modal_=nullptr;
    std::vector<uint16_t> pixels_=std::vector<uint16_t>(128*128);
    std::vector<Message> messages_;
    json prefs_={{"volume",70},{"idle_timeout",45},{"overall_timeout",300},{"input_device",nullptr},{"output_device",nullptr},{"language","en"}};
    json devices_=json::array();
    int history_=0,setting_=0;
    std::string mode_="unpaired",status_="Starting",notice_,caption_id_,setup_url_,pair_name_,call_phase_="idle";
    uint32_t setup_until_=0;
    float level_=0,mode_at_=0;
    bool call_=false,muted_=false,paired_=false;
    uint32_t esc_at_=0,notice_until_=0;
    bool esc_down_=false;
    bool chinese()const{return prefs_.value("language",std::string("en"))=="zh-CN";}
    std::string tr(const char* en,const char* zh)const{return chinese()?zh:en;}
    std::string space_command()const{
        if(call_phase_=="stopping")return "";
        if(call_)return "call.stop";
        if(paired_&&(mode_=="connecting"||mode_=="offline"))return "connect";
        return mode_=="unpaired"?"pair":"call.start";
    }
    std::string footer_text()const{
        if(page=="setup"||page=="pairing")return tr("Same Wi-Fi   Esc Cancel","同一局域网   Esc 取消");
        if(page=="compose")return tr("Enter Send   Esc Cancel   English input","Enter 发送   Esc 取消   英文输入");
        if(page=="settings")return tr("F/X Select   Z/C Change   Enter OK   Esc Back","F/X 选择  Z/C 修改  Enter 确认");
        if(page=="history")return tr("F/X Scroll   Tab Home   Esc Back","F/X 翻阅   Tab 返回   Esc 返回");
        if(call_phase_=="stopping")return tr("Ending call...","正在结束语音……");
        if(call_)return tr("Space End   B Interrupt   M Mute","Space 挂断   B 打断   M 静音");
        if(paired_&&(mode_=="connecting"||mode_=="offline"))return tr("Space Retry Muse   S Settings","Space 重试 Muse   S 设置");
        if(mode_=="unpaired")return tr("Space Phone setup   S Settings","Space 手机连接   S 设置");
        return call_?tr("Space End   B Interrupt   M Mute","Space 挂断   B 打断   M 静音"):tr("Space Talk   Enter Type   S Settings","Space 通话   Enter 输入   S 设置");
    }
    void refresh_caption(){
        if(page!="home"||!caption_)return;
        if(paired_&&!call_&&(mode_=="connecting"||mode_=="offline")){
            lv_obj_set_style_text_font(role_,chinese()?font_:&lv_font_montserrat_10,0);
            lv_label_set_text(role_,tr("PAIRED","已配对").c_str());
            lv_label_set_text(caption_,status_.c_str());return;
        }
        if(messages_.empty()){
            lv_label_set_text(role_,"MUSER");
            auto hint=mode_=="unpaired"?tr("Press Space for\nphone setup.\n\nScan. Paste. Pair.","按空格开始\n手机连接\n\n扫码导入后\n蓝牙配对"):
                tr("Press Space to\ntalk to Muse.\n\nTab for this\nsession's messages.","按空格，与 Muse\n开始语音对话。\n\nTab 查看本次记录");
            lv_label_set_text(caption_,hint.c_str());return;
        }
        auto& m=messages_.back();
        lv_obj_set_style_text_font(role_,m.role=="user"?font_:&lv_font_montserrat_10,0);
        lv_label_set_text(role_,m.role=="user"?tr("YOU","我").c_str():"MUSE");
        lv_label_set_text(caption_,m.text.c_str());
        if(caption_id_!=m.id){lv_obj_scroll_to_y(scroll_,0,LV_ANIM_OFF);caption_id_=m.id;}
        else if(!m.final){lv_obj_update_layout(scroll_);lv_obj_scroll_to_y(scroll_,LV_COORD_MAX,LV_ANIM_OFF);}
    }
    void history_caption(){
        if(messages_.empty()){lv_label_set_text(header_,tr("Session | 0 messages","本次对话 · 0 条").c_str());lv_label_set_text(caption_,tr("No messages yet.\nPress Tab to go back and talk to Muse.","本次还没有对话。\n按 Tab 返回，与 Muse 说句话。").c_str());return;}
        history_=std::clamp(history_,0,int(messages_.size())-1);
        auto& m=messages_[history_];
        lv_label_set_text(caption_,m.text.c_str());
        std::string h=tr("Session  ","本次对话  ")+std::to_string(history_+1)+"/"+std::to_string(messages_.size())+tr(" | ","  · ")+(m.role=="user"?tr("You","我"):"Muse");
        lv_label_set_text(header_,h.c_str());lv_obj_scroll_to_y(scroll_,0,LV_ANIM_OFF);
    }
    void refresh_settings(){
        if(page!="settings")return;
        auto name=[&](const char* k){
            if(prefs_[k].is_null())return tr("System default","系统默认");
            for(auto& d:devices_)if(d["id"]==prefs_[k])return d.value("name",tr("Audio device","音频设备"));
            return tr("Device ","设备 ")+prefs_[k].dump();
        };
        std::vector<std::pair<std::string,std::string>> rows={
            {tr("Phone setup","手机连接"),paired_?tr("Paired","已配对"):tr("Start >","开始 >")},
            {tr("Microphone","输入设备"),name("input_device")},
            {tr("Speaker","输出设备"),name("output_device")},
            {tr("Volume","播放音量"),prefs_["volume"].dump()+"%"},
            {tr("Microphone mute","麦克风静音"),muted_?tr("On","开启"):tr("Off","关闭")},
            {tr("Idle timeout","空闲挂断"),prefs_["idle_timeout"].dump()+tr(" sec"," 秒")},
            {tr("Call limit","通话上限"),prefs_["overall_timeout"].dump()+tr(" sec"," 秒")},
            {tr("Clear session","清空本次记录"),tr("Clear >","清空 >")},
            {tr("Reconnect","重新连接"),tr("Retry >","重试 >")},
            {tr("Language","界面语言"),chinese()?"简体中文":"English"}};
        lv_obj_clean(settings_rows_);
        auto section=setting_==0||setting_==8?tr("CONNECTION","连接"):setting_<5?tr("AUDIO","音频"):setting_<8?tr("CONVERSATION","会话"):tr("GENERAL","通用");
        label(settings_rows_,chinese()?font_:&lv_font_montserrat_10,10,0,250,section.c_str(),DIM);
        int first=std::clamp(setting_-2,0,5);
        for(int slot=0;slot<5;++slot){
            int i=first+slot;bool active=i==setting_;
            auto row=plain(settings_rows_,8,17+slot*22,304,21,active?0x142d46:BG);
            lv_obj_set_style_radius(row,3,0);
            if(active){lv_obj_set_style_border_width(row,2,0);lv_obj_set_style_border_side(row,LV_BORDER_SIDE_LEFT,0);lv_obj_set_style_border_color(row,color(ACCENT),0);}
            auto title=label(row,font_,8,0,180,rows[i].first.c_str(),active?TEXT:0xb0bac6);
            lv_obj_align(title,LV_ALIGN_LEFT_MID,8,0);lv_label_set_long_mode(title,LV_LABEL_LONG_DOT);
            auto value=label(row,chinese()?font_:&lv_font_montserrat_10,192,0,102,rows[i].second.c_str(),active?ACCENT:DIM);
            lv_obj_set_style_text_align(value,LV_TEXT_ALIGN_RIGHT,0);lv_obj_align(value,LV_ALIGN_RIGHT_MID,-8,0);
            lv_label_set_long_mode(value,LV_LABEL_LONG_DOT);
        }
    }
    void adjust_setting(int direction){
        json values=json::object();
        if(setting_==3)values["volume"]=std::clamp(prefs_["volume"].get<int>()+direction*5,0,100);
        else if(setting_==9)values["language"]=chinese()?"en":"zh-CN";
        else if(setting_==4){muted_=!muted_;bridge_.send({{"command","mic.mute"},{"muted",muted_}});}
        else if(setting_==5||setting_==6){
            const char* k=setting_==5?"idle_timeout":"overall_timeout";
            values[k]=std::clamp(prefs_[k].get<int>()+direction*(setting_==5?15:60),setting_==5?15:60,setting_==5?3600:7200);
        }else if(setting_==1||setting_==2){
            const char* key=setting_==1?"input_device":"output_device";
            const char* type=setting_==1?"input":"output";
            std::vector<json> ids={nullptr};for(auto& d:devices_)if(d.value(type,false))ids.push_back(d["id"]);
            auto it=std::find(ids.begin(),ids.end(),prefs_[key]);int n=it==ids.end()?0:int(it-ids.begin());
            values[key]=ids[(n+direction+int(ids.size()))%int(ids.size())];
        }
        if(!values.empty()){
            prefs_.update(values);bridge_.send({{"command","settings"},{"values",values}});
            if(values.contains("language")){font_=chinese()?cjk_:&lv_font_montserrat_12;show(page);}
            if(call_&&(setting_==1||setting_==2))toast(tr("Audio device changes apply next call","音频设备将在下次通话生效"));
        }
        refresh_settings();
    }
public:
    std::string page="home";
    bool running=true;
    UI(Bridge& bridge,const lv_font_t* font):bridge_(bridge),font_(&lv_font_montserrat_12),cjk_(font){
        root_=lv_screen_active();lv_obj_remove_style_all(root_);lv_obj_set_style_bg_color(root_,color(BG),0);
        lv_obj_set_style_bg_opa(root_,LV_OPA_COVER,0);lv_obj_remove_flag(root_,LV_OBJ_FLAG_SCROLLABLE);
        header_=label(root_,font_,8,1,304,"");
        footer_=label(root_,font_,6,147,310,"",DIM);
        lv_obj_set_style_text_font(footer_,&lv_font_montserrat_10,0);
        show("home");muse_pixel_set_size(128);
    }
    void toast(const std::string& text){notice_=text;notice_until_=ticks()+4000;}
    void message(const std::string& text){
        if(modal_)lv_obj_delete(modal_);
        modal_=plain(root_,0,18,320,128,0x171d27);
        label(modal_,cjk_,10,8,300,text.c_str());
    }
    void show(const std::string& next){
        page=next;if(body_)lv_obj_delete(body_);
        body_=plain(root_,0,18,320,128);avatar_=caption_=role_=scroll_=editor_=settings_rows_=nullptr;
        lv_obj_set_style_text_font(footer_,font_,0);
        if(!chinese())lv_obj_set_style_text_font(footer_,&lv_font_montserrat_10,0);
        lv_obj_set_style_text_font(header_,font_,0);
        lv_label_set_text(footer_,footer_text().c_str());
        if(page=="home"){
            avatar_=lv_canvas_create(body_);lv_obj_set_pos(avatar_,0,0);
            lv_canvas_set_buffer(avatar_,pixels_.data(),128,128,LV_COLOR_FORMAT_RGB565);
            role_=label(body_,&lv_font_montserrat_10,138,5,174,"MUSER",ACCENT);
            scroll_=plain(body_,134,23,180,103);lv_obj_add_flag(scroll_,LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_scrollbar_mode(scroll_,LV_SCROLLBAR_MODE_OFF);
            caption_=label(scroll_,cjk_,4,0,172,"");refresh_caption();
        }else if(page=="setup"){
            auto address=label(body_,&lv_font_montserrat_18,8,1,304,setup_url_.c_str(),ACCENT);
            lv_obj_set_style_text_align(address,LV_TEXT_ALIGN_CENTER,0);
            lv_label_set_long_mode(address,LV_LABEL_LONG_CLIP);
            auto qr=lv_qrcode_create(body_);lv_qrcode_set_size(qr,88);lv_qrcode_set_quiet_zone(qr,true);
            lv_qrcode_set_dark_color(qr,lv_color_black());lv_qrcode_set_light_color(qr,lv_color_white());
            lv_obj_set_pos(qr,8,34);lv_qrcode_update(qr,setup_url_.data(),setup_url_.size());
            label(body_,font_,112,35,200,tr("Scan QR or enter\nthis address in any\nphone / PC browser.\nPaste token to pair.","扫码或浏览器打开\n上方地址，粘贴token\n然后自动蓝牙配对").c_str());
            label(body_,chinese()?font_:&lv_font_montserrat_10,112,106,200,tr("Same network as this device","手机 / 电脑与设备同网").c_str(),DIM);
        }else if(page=="pairing"){
            label(body_,chinese()?font_:&lv_font_montserrat_10,10,6,300,tr("BLUETOOTH PAIRING","蓝牙配对").c_str(),ACCENT);
            label(body_,font_,10,24,300,(pair_name_.empty()?tr("Starting Bluetooth pairing...\nPlease wait for the device name.","正在开启蓝牙配对……\n请等待设备名称出现。"):tr("Muse app > Settings > Devices\nTurn on Developer mode. Add device:","Muse App → 设置 → 设备\n开启开发者模式，添加以下设备：")).c_str());
            label(body_,cjk_,10,80,300,pair_name_.c_str());
            if(!pair_name_.empty())label(body_,chinese()?font_:&lv_font_montserrat_10,10,104,300,tr("If asked for Wi-Fi, select the current network.","如出现 Wi-Fi 选择，选择当前网络。").c_str(),DIM);
        }else if(page=="history"){
            scroll_=plain(body_,8,4,304,120);lv_obj_add_flag(scroll_,LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_scrollbar_mode(scroll_,LV_SCROLLBAR_MODE_OFF);
            caption_=label(scroll_,cjk_,0,0,298,"");history_=int(messages_.size())-1;history_caption();
        }else if(page=="compose"){
            label(body_,font_,8,3,304,tr("English, numbers and symbols","输入英文、数字和符号").c_str(),DIM);
            editor_=lv_textarea_create(body_);lv_obj_set_pos(editor_,8,27);lv_obj_set_size(editor_,304,92);
            lv_obj_set_style_text_font(editor_,font_,0);lv_obj_set_style_text_color(editor_,color(TEXT),0);
            lv_obj_set_style_bg_color(editor_,color(0x151c25),0);lv_obj_set_style_border_width(editor_,1,0);
            lv_obj_set_style_border_color(editor_,color(0x57496a),0);lv_obj_set_style_radius(editor_,3,0);
            lv_textarea_set_max_length(editor_,1024);lv_textarea_set_placeholder_text(editor_,"Type a message...");
            lv_obj_add_state(editor_,LV_STATE_FOCUSED);
        }else{
            settings_rows_=plain(body_,0,0,320,128);
            refresh_settings();bridge_.send({{"command","settings"}});
        }
    }
    void event(const json& e){
        auto type=e.value("type",std::string());
        if(type=="pairing.state"){
            paired_=e.value("paired",false);
            if(paired_&&mode_=="unpaired")mode_="connecting";
            refresh_caption();refresh_settings();
        }else if(type=="pair.result"){
            auto outcome=e.value("outcome",std::string());
            if(outcome=="paired"||outcome=="already_paired"){
                paired_=true;mode_="connecting";status_=tr("Pairing complete. Connecting to Muse.","配对已完成，正在连接 Muse。");
                if(page=="pairing"||page=="setup")show("home");else refresh_caption();
            }else if(page=="pairing")show("home");
        }else if(type=="status"){
            auto mode=e.value("mode",std::string("idle"));if(mode_!=mode){mode_=mode;mode_at_=ticks()/1000.f;}
            status_=e.value("text",std::string());
            if(mode=="pairing"){pair_name_.clear();setup_until_=ticks()+60000;show("pairing");}
            if((page=="pairing"||page=="setup")&&(mode=="idle"||mode=="unpaired"))show("home");
            refresh_caption();
        }else if(type=="transcript"){
            std::string id=e.value("id",std::string()),text=e.value("text",std::string());
            if(id.empty()||text.size()>65536)return;
            auto it=std::find_if(messages_.begin(),messages_.end(),[&](auto& m){return m.id==id;});
            if(it==messages_.end())messages_.push_back({id,e.value("role",std::string("assistant")),text,e.value("final",false)});
            else{it->text=text;it->final=e.value("final",false);}
            size_t bytes=0;for(auto& m:messages_)bytes+=m.text.size();
            while(messages_.size()>256||bytes>2*1024*1024){bytes-=messages_.front().text.size();messages_.erase(messages_.begin());}
            refresh_caption();
        }else if(type=="level")level_=mode_=="speaking"?e.value("playback",0.f):e.value("mic",0.f);
        else if(type=="call"){
            call_=e.value("active",false);call_phase_=e.value("phase",std::string(call_?"live":"idle"));
        }
        else if(type=="muted"){muted_=e.value("value",false);refresh_settings();}
        else if(type=="settings"){
            auto old_language=prefs_["language"];
            for(auto& [key,value]:prefs_.items())if(e.contains(key))value=e[key];
            if(old_language!=prefs_["language"]){font_=chinese()?cjk_:&lv_font_montserrat_12;show(page);}
            devices_=e.value("devices",json::array());refresh_settings();
        }else if(type=="compose.ready")show("compose");
        else if(type=="cleared"){messages_.clear();caption_id_.clear();show("home");toast(tr("Session messages cleared","本次记录已清空"));}
        else if(type=="setup"){
            setup_url_=e.value("url",std::string());setup_until_=ticks()+e.value("expires",300)*1000;show("setup");
        }else if(type=="setup.closed"){
            if(page=="setup"||page=="pairing")show("home");
        }else if(type=="pairing"){
            pair_name_=e.value("name",std::string());setup_until_=ticks()+e.value("timeout",600)*1000;show("pairing");
        }else if(type=="error"){
            message(e.value("message",tr("Operation failed","操作失败")));
            if(e.value("code",std::string())=="backend_closed"){mode_="error";call_=false;}
        }
    }
    void text(const std::string& value){
        if(page!="compose"||!editor_)return;
        std::string ascii;for(unsigned char c:value)if(c>=32&&c<127)ascii+=char(c);
        lv_textarea_add_text(editor_,ascii.c_str());
    }
    void key(const KeyEvent& e){
        if(e.key==K_ESC){
            if(e.down){if(!esc_down_){esc_down_=true;esc_at_=ticks();}}
            else if(esc_down_){esc_down_=false;if(ticks()-esc_at_<700){
                if(modal_){lv_obj_delete(modal_);modal_=nullptr;}
                else if(page=="setup"||page=="pairing"){bridge_.send({{"command","pair.cancel"}});show("settings");}
                else if(page!="home")show("home");else if(call_)bridge_.send({{"command","call.stop"}});
                else toast(tr("Hold Esc to exit Muser","长按 Esc 退出 Muser"));
            }}return;
        }
        if(!e.down)return;
        if(modal_){if(e.key==K_ENTER){lv_obj_delete(modal_);modal_=nullptr;}return;}
        if(page=="setup"||page=="pairing")return;
        if(page=="compose"){
            if(e.key==K_ENTER){std::string value=lv_textarea_get_text(editor_);if(!value.empty()){
                bridge_.send({{"command","chat.send"},{"text",value}});show("home");}}
            else if(e.key==K_BACKSPACE)lv_textarea_delete_char(editor_);
            else if(e.key==K_LEFT)lv_textarea_cursor_left(editor_);
            else if(e.key==K_RIGHT)lv_textarea_cursor_right(editor_);
            else if(e.key==K_DELETE)lv_textarea_delete_char_forward(editor_);
            else text(e.text);
            return;
        }
        int routed=e.key;
        switch(routed){case 'f':case 'F':routed=K_UP;break;case 'x':case 'X':routed=K_DOWN;break;case 'z':case 'Z':routed=K_LEFT;break;case 'c':case 'C':routed=K_RIGHT;break;default:break;}
        if(routed==K_UP||routed==K_DOWN){
            int dir=routed==K_UP?-1:1;
            if(page=="settings"){setting_=(setting_+dir+10)%10;refresh_settings();}
            else if(scroll_){
                lv_obj_update_layout(scroll_);
                if(page=="history"&&((dir<0&&lv_obj_get_scroll_y(scroll_)<=0)||(dir>0&&lv_obj_get_scroll_bottom(scroll_)<=0))){
                    history_+=dir;history_caption();
                }else lv_obj_scroll_by(scroll_,0,-dir*48,LV_ANIM_OFF);
            }return;
        }
        if(e.repeat)return;
        int k=routed;if(k>='A'&&k<='Z')k+=32;
        if(page=="settings"){
            if(k==K_LEFT||k==K_RIGHT)adjust_setting(k==K_LEFT?-1:1);
            else if(k==K_ENTER){
                if(setting_==0)bridge_.send({{"command","pair"}});
                else if(setting_==7)bridge_.send({{"command","clear"}});
                else if(setting_==8)bridge_.send({{"command","connect"}});
                else adjust_setting(1);
            }return;
        }
        if(k==K_TAB)show(page=="history"?"home":"history");
        else if(k==K_ENTER)bridge_.send({{"command","compose"}});
        else if(k=='s')show("settings");
        else if(k==' '){auto command=space_command();if(!command.empty())bridge_.send({{"command",command}});}
        else if(k=='b')bridge_.send({{"command","call.interrupt"}});
        else if(k=='m'){muted_=!muted_;bridge_.send({{"command","mic.mute"},{"muted",muted_}});}
        else if(k==K_LEFT||k==K_RIGHT){
            int volume=std::clamp(prefs_["volume"].get<int>()+(k==K_LEFT?-5:5),0,100);prefs_["volume"]=volume;
            bridge_.send({{"command","settings"},{"values",{{"volume",volume}}}});toast(tr("Volume ","音量 ")+std::to_string(volume)+"%");
        }
    }
    void frame(){
        if(esc_down_&&ticks()-esc_at_>=700)running=false;
        if(page!="history"){
            std::string title=page=="settings"?tr("Settings  ","设置  ")+std::to_string(setting_+1)+"/10":page=="compose"?tr("Muser | Text chat","Muser · 文字聊天"):page=="setup"?tr("Muser | Phone setup","Muser · 手机设置"):page=="pairing"?tr("Muser | Pair phone","Muser · 蓝牙配对"):"Muser";
            if(page=="home")title+=tr(" | ","  · ")+(mode_=="unpaired"?tr("Not paired","未配对"):mode_=="offline"?tr("Paired / retrying","已配对 / 重试中"):mode_=="connecting"?tr("Connecting Muse","连接 Muse 中"):mode_=="speaking"?tr("Speaking","说话中"):mode_=="listening"?tr("Listening","聆听中"):mode_=="thinking"?tr("Thinking","思考中"):mode_=="error"?tr("Error","连接异常"):tr("Ready","就绪"));
            if(page=="home"&&call_phase_=="starting")title=tr("Muser | Starting voice","Muser · 正在开启语音");
            if(page=="home"&&call_phase_=="stopping")title=tr("Muser | Ending voice","Muser · 正在结束语音");
            if(muted_)title+=tr(" | Muted"," · 静音");
            lv_label_set_text(header_,title.c_str());
        }
        std::string hint=modal_?tr("Enter Close   Esc Back","Enter 关闭   Esc 返回"):ticks()<notice_until_?notice_:footer_text();
        if(!modal_&&(page=="setup"||page=="pairing"))hint=tr("Esc Cancel   Expires in ","Esc 取消   剩余 ")+std::to_string(setup_until_>ticks()?(setup_until_-ticks())/1000:0)+tr("s","秒");
        lv_label_set_text(footer_,hint.c_str());
        if(avatar_){
            muse_mode_t mode=MUSE_MODE_IDLE;
            if(mode_=="connecting"||mode_=="offline")mode=MUSE_MODE_BOOT;
            else if(mode_=="listening")mode=MUSE_MODE_LISTENING;
            else if(mode_=="thinking")mode=MUSE_MODE_THINKING;
            else if(mode_=="speaking")mode=MUSE_MODE_SPEAKING;
            else if(mode_=="error")mode=MUSE_MODE_ERROR;
            float now=ticks()/1000.f;
            muse_pose_t pose{mode,now,now-mode_at_,level_,0,0};
            muse_pixel_render(&pose);muse_pixel_scale(pixels_.data(),128,0,127,0,127);lv_obj_invalidate(avatar_);
        }
    }
    void capture(const fs::path& path){
        lv_obj_update_layout(root_);
        auto buf=lv_snapshot_take(root_,LV_COLOR_FORMAT_ARGB8888);
        if(!buf)throw std::runtime_error("Screenshot failed");
        std::vector<unsigned char> rgb(320*170*3);
        for(int y=0;y<170;y++)for(int x=0;x<320;x++){
            auto p=buf->data+y*buf->header.stride+x*4;auto i=(y*320+x)*3;
            rgb[i]=p[2];rgb[i+1]=p[1];rgb[i+2]=p[0];
        }
        png_image image{};image.version=PNG_IMAGE_VERSION;image.width=320;image.height=170;image.format=PNG_FORMAT_RGB;
        fs::create_directories(path.parent_path());
        bool ok=png_image_write_to_file(&image,path.c_str(),0,rgb.data(),0,nullptr);
        lv_draw_buf_destroy(buf);if(!ok)throw std::runtime_error("PNG write failed");
    }
    void test_composer(){
        if(chinese())throw std::runtime_error("Muser must default to English");
        event({{"type","pairing"},{"name","MuseGadgetTEST00"},{"timeout",600}});
        if(page!="pairing")throw std::runtime_error("Pairing page did not open");
        event({{"type","pair.result"},{"outcome","paired"}});
        if(page!="home"||!paired_||mode_!="connecting")throw std::runtime_error("Pair result left UI on pairing page");
        event({{"type","status"},{"mode","offline"},{"text","Paired. Cannot reach Muse service; retrying."}});
        if(page!="home"||!paired_||footer_text().find("Retry")==std::string::npos)throw std::runtime_error("Cloud retry was confused with Bluetooth pairing");
        frame();capture(fs::path(root_path())/".cache/ui-paired-retry.png");
        event({{"type","status"},{"mode","idle"},{"text","Demo"}});
        event({{"type","call"},{"active",true},{"phase","starting"}});
        event({{"type","status"},{"mode","connecting"},{"text","Starting voice"}});
        if(space_command()!="call.stop"||footer_text().find("End")==std::string::npos)throw std::runtime_error("Voice startup Space incorrectly reconnects Muse");
        event({{"type","call"},{"active",true},{"phase","stopping"}});
        if(!space_command().empty())throw std::runtime_error("Space queued a new command during hangup");
        event({{"type","call"},{"active",false},{"phase","idle"}});
        event({{"type","status"},{"mode","idle"},{"text","Demo"}});
        if(space_command()!="call.start")throw std::runtime_error("Voice redial did not recover after hangup");
        show("settings");setting_=0;key({'x',true,"x"});
        if(setting_!=1)throw std::runtime_error("Fn-free down navigation failed");
        key({'f',true,"f"});if(setting_!=0)throw std::runtime_error("Fn-free up navigation failed");
        setting_=3;int volume=prefs_["volume"].get<int>();key({'z',true,"z"});key({'c',true,"c"});
        if(prefs_["volume"]!=volume)throw std::runtime_error("Fn-free value adjustment failed");
        setting_=9;adjust_setting(1);
        if(!chinese()||font_!=cjk_)throw std::runtime_error("Chinese language switch failed");
        frame();capture(fs::path(root_path())/".cache/ui-zh-CN.png");
        event({{"type","setup"},{"url","http://192.168.1.42:8050/"},{"expires",300}});
        frame();capture(fs::path(root_path())/".cache/ui-setup-zh-CN.png");
        show("settings");
        adjust_setting(1);
        if(chinese()||font_!=&lv_font_montserrat_12)throw std::runtime_error("English language switch failed");
        frame();
        for(unsigned char c:std::string(lv_label_get_text(header_)))if(c>=128)throw std::runtime_error("Unsupported English chrome glyph");
        setting_=0;
        show("compose");key({'f',true,"f"});key({'x',true,"x"});key({'z',true,"z"});key({'c',true,"c"});
        if(std::string(lv_textarea_get_text(editor_))!="fxzc")throw std::runtime_error("Navigation letters lost in text input");
        lv_textarea_set_text(editor_,"");text("b m s test 123!");
        if(std::string(lv_textarea_get_text(editor_))!="b m s test 123!"||muted_)throw std::runtime_error("Composer shortcut routing failed");
        key({K_LEFT,true,""});key({K_BACKSPACE,true,""});
        if(std::string(lv_textarea_get_text(editor_))!="b m s test 12!")throw std::runtime_error("Composer cursor editing failed");
        key({K_ENTER,true,""});
    }
};

int main(int argc,char** argv){
    bool demo=false,capture=false,self_test=false;std::string capture_dir="screenshots";
    for(int i=1;i<argc;i++){
        std::string arg=argv[i];if(arg=="--demo")demo=true;
        else if(arg=="--capture-all"){capture=true;demo=true;if(i+1<argc)capture_dir=argv[++i];}
        else if(arg=="--self-test"){demo=true;self_test=true;}
        else if(arg=="--help"){std::cout<<"Muser [--demo] [--capture-all DIRECTORY] [--self-test]\n";return 0;}
        else{std::cerr<<"Unknown Muser option\n";return 2;}
    }
    signal(SIGINT,stop_signal);signal(SIGTERM,stop_signal);
    try{
        lv_init();lv_tick_set_cb(ticks);
        auto root=root_path();
#if USE_DESKTOP
        if(SDL_Init(SDL_INIT_VIDEO|SDL_INIT_EVENTS|SDL_INIT_TIMER))throw std::runtime_error(SDL_GetError());
        auto window=SDL_CreateWindow("Muser",SDL_WINDOWPOS_CENTERED,SDL_WINDOWPOS_CENTERED,960,510,SDL_WINDOW_RESIZABLE|SDL_WINDOW_ALLOW_HIGHDPI);
        if(!window)throw std::runtime_error(SDL_GetError());
        auto renderer=SDL_CreateRenderer(window,-1,SDL_RENDERER_SOFTWARE);
        if(!renderer)throw std::runtime_error(SDL_GetError());
        SDL_RenderSetLogicalSize(renderer,320,170);
        auto texture=SDL_CreateTexture(renderer,SDL_PIXELFORMAT_ARGB8888,SDL_TEXTUREACCESS_STREAMING,320,170);
        auto display=lv_display_create(320,170);
        std::vector<uint32_t> fb(320*170);
        lv_display_set_buffers(display,fb.data(),nullptr,fb.size()*4,LV_DISPLAY_RENDER_MODE_FULL);
        lv_display_set_user_data(display,texture);
        lv_display_set_flush_cb(display,[](lv_display_t* d,const lv_area_t*,uint8_t* data){
            SDL_UpdateTexture(static_cast<SDL_Texture*>(lv_display_get_user_data(d)),nullptr,data,320*4);lv_display_flush_ready(d);
        });
        SDL_StartTextInput();
#else
        auto display=lv_linux_fbdev_create();std::string fb;
        if(auto e=getenv("LV_LINUX_FBDEV_DEVICE"))fb=e;
        else if(fs::exists("/dev/fb_lcd"))fb="/dev/fb_lcd";
        else{std::ifstream f("/proc/fb");std::string line;while(std::getline(f,line))if(line.find("fb_st7789v")!=std::string::npos||line.find("panel-mipi-dbid")!=std::string::npos){fb="/dev/fb"+line.substr(0,line.find(' '));break;}}
        if(fb.empty())throw std::runtime_error("Cardputer LCD not found; set LV_LINUX_FBDEV_DEVICE");
        lv_linux_fbdev_set_file(display,fb.c_str());
        if(lv_display_get_horizontal_resolution(display)!=320||lv_display_get_vertical_resolution(display)!=170)throw std::runtime_error("Expected 320x170 framebuffer");
#endif
        auto font=lv_freetype_font_create((root+"/assets/fonts/NotoSansCJKsc-Regular.otf").c_str(),LV_FREETYPE_FONT_RENDER_MODE_BITMAP,14,LV_FREETYPE_FONT_STYLE_NORMAL);
        if(!font)throw std::runtime_error("Muser CJK font missing");
        {
            Bridge bridge(root,demo);Input input;UI ui(bridge,font);int stage=0;
            while(ui.running&&!interrupted){
                for(auto& item:bridge.poll())ui.event(item);
                input.poll([&](KeyEvent e){ui.key(e);});
#if USE_DESKTOP
                SDL_Event e;while(SDL_PollEvent(&e)){
                    if(e.type==SDL_QUIT)ui.running=false;
                    if(e.type==SDL_TEXTINPUT)ui.text(e.text.text);
                    if(e.type==SDL_KEYDOWN||e.type==SDL_KEYUP){
                        int k=0;switch(e.key.keysym.sym){
                            case SDLK_ESCAPE:k=K_ESC;break;case SDLK_RETURN:k=K_ENTER;break;
                            case SDLK_BACKSPACE:k=K_BACKSPACE;break;case SDLK_TAB:k=K_TAB;break;
                            case SDLK_UP:k=K_UP;break;case SDLK_DOWN:k=K_DOWN;break;
                            case SDLK_LEFT:k=K_LEFT;break;case SDLK_RIGHT:k=K_RIGHT;break;
                            case SDLK_DELETE:k=K_DELETE;break;default:if(e.key.keysym.sym<128)k=e.key.keysym.sym;
                        }
                        if(k)ui.key({k,e.type==SDL_KEYDOWN,"",bool(e.key.repeat)});
                    }
                }
#endif
                auto t=ticks();
                if(capture&&t>uint32_t(500+stage*350)){
                    if(stage==0){ui.frame();ui.capture(fs::path(capture_dir)/"home.png");}
                    if(stage==1){ui.event({{"type","status"},{"mode","speaking"},{"text","Muse 正在说话"}});
                        ui.event({{"type","call"},{"active",true}});
                        ui.event({{"type","transcript"},{"id","sample"},{"role","assistant"},{"text","你好！我是 Muse。\n今天想聊些什么？\n\n我们可以聊灵感、计划，或者你正在做的项目。"},{"final",true}});
                        ui.event({{"type","level"},{"playback",.65f}});ui.frame();ui.capture(fs::path(capture_dir)/"speaking.png");}
                    if(stage==2){ui.show("history");ui.frame();ui.capture(fs::path(capture_dir)/"history.png");}
                    if(stage==3){ui.show("settings");ui.frame();ui.capture(fs::path(capture_dir)/"settings.png");}
                    if(stage==4){ui.event({{"type","setup"},{"url","http://192.168.1.42:8050/"},{"expires",300}});ui.frame();ui.capture(fs::path(capture_dir)/"setup.png");ui.running=false;}
                    ++stage;
                }
                if(self_test&&stage==0&&t>400){ui.test_composer();stage=1;}
                if(self_test&&stage==1&&t>1000){
                    ui.key({K_TAB,true,""});if(ui.page!="history")throw std::runtime_error("History routing failed");
                    ui.key({K_ESC,true,""});ui.key({K_ESC,false,""});
                    if(ui.page!="home")throw std::runtime_error("Short Esc exited instead of back");
                    ui.key({K_ESC,true,""});stage=2;
                }
                if(self_test&&t>3000)throw std::runtime_error("Long Esc did not exit");
                ui.frame();lv_timer_handler();
#if USE_DESKTOP
                SDL_RenderClear(renderer);SDL_RenderCopy(renderer,texture,nullptr,nullptr);SDL_RenderPresent(renderer);
#endif
                std::this_thread::sleep_for(std::chrono::milliseconds(16));
            }
        }
        lv_obj_clean(lv_screen_active());lv_freetype_font_delete(font);lv_deinit();
#if USE_DESKTOP
        SDL_DestroyTexture(texture);SDL_DestroyRenderer(renderer);SDL_DestroyWindow(window);SDL_Quit();
#endif
        if(self_test)std::cout<<"Muser UI smoke checks passed\n";
        return 0;
    }catch(const std::exception& e){std::cerr<<"Muser: "<<e.what()<<"\n";return 1;}
}
