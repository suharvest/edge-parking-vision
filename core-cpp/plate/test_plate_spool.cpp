#include "plate_spool.h"
#include <cassert>
#include <cerrno>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
using namespace vb::plate;
namespace {
int fail_fsync_calls = 0;
int fail_dir_fsync_calls = 0;
int rename_calls = 0;
int fail_rename_call = 0;
extern "C" int __real_fsync(int);
extern "C" int __real_rename(const char*, const char*);
extern "C" int __wrap_fsync(int fd){
 struct stat st{}; if(::fstat(fd,&st)==0 && S_ISDIR(st.st_mode) && fail_dir_fsync_calls>0){--fail_dir_fsync_calls;errno=EIO;return -1;}
 if(fail_fsync_calls>0){--fail_fsync_calls;errno=EIO;return -1;} return __real_fsync(fd);
}
extern "C" int __wrap_rename(const char* from,const char* to){
 ++rename_calls; if(fail_rename_call>0&&rename_calls==fail_rename_call){errno=EIO;return -1;} return __real_rename(from,to);
}
}
namespace { vb::Json env(uint64_t n,bool replayed=false){return vb::Json{{"schema","parking.plate/1"},{"event_id","00000000-0000-4000-8000-"+std::string(12,'0')+std::to_string(n%10)},{"site_id","demo-lot"},{"camera_id","gate-a"},{"ts",1700000000000ULL+n},{"seq",n},{"replayed",replayed},{"plate","粤A12345"},{"plate_normalized","粤A12345"},{"region","cn"},{"plate_type","cn_blue"},{"confidence",0.9},{"votes",3},{"frames",4},{"bbox",vb::Json::array({.1,.2,.8,.9})},{"track_id",1},{"snapshot",nullptr}}; }
}
int main(){auto root=std::filesystem::temp_directory_path()/("plate-spool-"+std::to_string(getpid()));std::filesystem::remove_all(root);std::filesystem::create_directories(root);
 auto p=root/"events.jsonl"; {Spool s(p.string(),1000);for(uint64_t i=0;i<1000;++i)assert(s.append("demo-lot/parking/gate-a/plate",env(i))==i+1);auto fresh=s.pending();assert(fresh.front().replayed==false&&fresh.front().envelope["replayed"]==false);assert(s.depth()==1000&&!s.overflow());}
 {Spool s(p.string(),1000);auto q=s.pending();assert(q.size()==1000&&q.front().id==1&&q.front().replayed==true&&q.front().envelope["replayed"]==true&&q[999].envelope["event_id"]==env(999)["event_id"]);bool nonhead=false;try{s.ack(2);}catch(const std::invalid_argument&){nonhead=true;}assert(nonhead);s.ack(1);assert(s.depth()==999);}
 {Spool s(p.string(),1000);assert(s.pending().front().id==2&&s.pending().front().envelope["seq"]==1);}
 auto ov=root/"overflow.jsonl";{Spool s(ov.string(),2);s.append("t",env(1));s.append("t",env(2));s.append("t",env(3));assert(s.depth()==3&&s.overflow());}
 auto busy=root/"busy.jsonl";{Spool s(busy.string(),3);pid_t pid=fork();assert(pid>=0);if(pid==0){try{Spool other(busy.string(),3);_exit(3);}catch(const std::runtime_error&){_exit(0);}}int st=0;assert(waitpid(pid,&st,0)==pid&&WIFEXITED(st)&&WEXITSTATUS(st)==0);}
 auto fail=root/"write.jsonl";{Spool s(fail.string(),3);s.append("t",env(1));std::filesystem::remove(fail);std::filesystem::create_directory(fail);bool threw=false;try{s.append("t",env(2));}catch(const std::runtime_error&){threw=true;}assert(threw&&s.depth()==1);std::filesystem::remove(fail);}
 auto fault=root/"fault.jsonl";{Spool s(fault.string(),3);fail_fsync_calls=1;bool threw=false;try{s.append("t",env(1));}catch(const std::runtime_error&){threw=true;}assert(threw&&s.poisoned());bool blocked=false;try{s.append("t",env(2));}catch(const std::runtime_error&){blocked=true;}assert(blocked);fail_fsync_calls=0;}
 auto dirfault=root/"dirfault.jsonl";{Spool s(dirfault.string(),3);fail_dir_fsync_calls=1;bool threw=false;try{s.append("t",env(1));}catch(const std::runtime_error&){threw=true;}assert(threw&&s.poisoned());fail_dir_fsync_calls=0;}{Spool s(dirfault.string(),3);assert(s.append("t",env(2))==2);}
 auto compactfault=root/"compactfault.jsonl";{Spool s(compactfault.string(),3);s.append("t",env(1));s.append("t",env(2));rename_calls=0;fail_rename_call=2;bool threw=false;try{s.ack(1);}catch(const std::runtime_error&){threw=true;}assert(threw&&s.poisoned());bool blocked=false;try{s.pending();}catch(const std::runtime_error&){blocked=true;}assert(blocked);fail_rename_call=0;}{Spool s(compactfault.string(),3);assert(s.pending().front().id==2);}
 auto bad=root/"bad.jsonl";{std::ofstream f(bad);f<<"{\"id\":1,\"topic\":\"t\",\"payload\":{}}\nnot-json\n";}bool corrupt=false;try{Spool s(bad.string(),3);}catch(const std::runtime_error&){corrupt=true;}assert(corrupt);
 auto tail=root/"tail.jsonl";{std::ofstream f(tail);f<<"{\"id\":1,\"topic\":\"t\",\"payload\":{}}";}bool incomplete=false;try{Spool s(tail.string(),3);}catch(const std::runtime_error&){incomplete=true;}assert(incomplete);
 auto maxid=root/"maxid.jsonl";{std::ofstream f(maxid);f<<vb::Json{{"id",UINT64_MAX},{"topic","t"},{"payload",vb::Json::object()}}.dump()<<"\n";}bool max_overflow=false;try{Spool s(maxid.string(),3);}catch(const std::overflow_error&){max_overflow=true;}assert(max_overflow);
 std::filesystem::remove_all(root);std::cout<<"plate_spool PASS fresh_replay=1 restart_pending=1000 head_ack=1 nonhead_reject=1 overflow_preserve=1 owner_failfast=1 write_failure=1 poison_fsync=1 poison_dirsync=1 compact_failure_recovery=1 corrupt_reject=1 tail_reject=1 id_overflow=1\n";}
