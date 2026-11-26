/* GPL-2.0-or-later. PhotoRec's validators read a logical file backed by
 * its source extents. No candidate is materialized by the scanner. */
#undef fread
#undef fseek
#undef ftell
#undef ftello
#undef feof
#include <stdint.h>
#include <fcntl.h>
#include <io.h>
#include <zlib.h>

static FILE marker;
static disk_t *medium;
static file_recovery_t *current;
static uint64_t pos, length;
static int at_eof;
static uLong full_crc, previous_crc;
static unsigned int last_size;
static unsigned char signature[128];
static size_t signature_size;

FILE *catalog_begin(file_recovery_t *file, disk_t *disk)
{
  medium=disk;
  current=file;
  pos=length=signature_size=last_size=0;
  at_eof=0;
  full_crc=previous_crc=0;
  return &marker;
}

void catalog_append(const unsigned char *bytes, unsigned int count)
{
  if(length==0) {
    signature_size=count<sizeof(signature)?count:sizeof(signature);
    memcpy(signature, bytes, signature_size);
  }
  previous_crc=full_crc;
  full_crc=crc32(full_crc, bytes, count);
  last_size=count;
  length+=count;
  pos=length;
}

int catalog_seek(FILE *stream, long long offset, int origin)
{
  if(stream!=&marker)
    return _fseeki64(stream, offset, origin);
  int64_t base=origin==SEEK_END?(int64_t)length:origin==SEEK_CUR?(int64_t)pos:0;
  if(offset < -base || (offset>0 && base>INT64_MAX-offset)) return -1;
  pos=base+offset;
  at_eof=0;
  return 0;
}

long long catalog_tell(FILE *stream)
{
  return stream==&marker?(long long)pos:_ftelli64(stream);
}

int catalog_eof(FILE *stream)
{
  return stream==&marker?at_eof:feof(stream);
}

size_t catalog_read(void *out, size_t size, size_t count, FILE *stream)
{
  if(stream!=&marker) return fread(out,size,count,stream);
  if(!size || count>SIZE_MAX/size) return 0;
  size_t wanted=size*count, done=0;
  uint64_t logical=0;
  const struct td_list_head *p;
  td_list_for_each(p,&current->location.list) {
    const alloc_list_t *run=td_list_entry_const(p,const alloc_list_t,list);
    if(!run->data) continue;
    uint64_t span=run->end-run->start+1;
    while(pos>=logical && pos-logical<span && done<wanted && pos<length) {
      uint64_t within=pos-logical, avail=span-within;
      unsigned int n=wanted-done>1048576?1048576:(unsigned int)(wanted-done);
      if(n>avail) n=(unsigned int)avail;
      if(n>length-pos) n=(unsigned int)(length-pos);
      if(medium->pread(medium,(unsigned char*)out+done,n,run->start+within)!=(int)n) {
        printf("DGREADERR\t%llu\n", (unsigned long long)(run->start + within));
        fflush(stdout);
        at_eof=1;
        return done/size;
      }
      done+=n;
      pos+=n;
    }
    if(done==wanted) break;
    logical+=span;
  }
  at_eof=done<wanted;
  return done/size;
}

void catalog_emit(file_recovery_t *file)
{
  if(!file->file_stat->file_hint->recover || file!=current ||
      !file->file_size || file->file_size>length) return;
  uint64_t remain=file->file_size;
  uLong crc=full_crc;
  if(remain!=length) {
    uint64_t prefix=length-last_size;
    pos=remain>=prefix?prefix:0;
    crc=pos?previous_crc:0;
    unsigned char buffer[65536];
    while(pos<remain) {
      size_t n=remain-pos>sizeof(buffer)?sizeof(buffer):(size_t)(remain-pos);
      if(catalog_read(buffer,1,n,&marker)!=n) return;
      crc=crc32(crc,buffer,n);
    }
  }
  const char *ext=file->extension?file->extension:file->file_stat->file_hint->extension;
  printf("DGFILE\t%s\t%llu\t%lld\t%lu\t",ext?ext:"bin",(unsigned long long)remain,(long long)file->time,crc);
  for(size_t i=0;i<signature_size && i<remain;i++) printf("%02x",signature[i]);
  const struct td_list_head *p;
  td_list_for_each(p,&file->location.list) {
    const alloc_list_t *run=td_list_entry_const(p,const alloc_list_t,list);
    if(!run->data || !remain) continue;
    uint64_t n=run->end-run->start+1;
    if(n>remain) n=remain;
    printf("\t%llu,%llu",(unsigned long long)run->start,(unsigned long long)n);
    remain-=n;
  }
  putchar('\n');
  fflush(stdout);
}

/* icat streams the mounted NTFS volume's $Bitmap. A missing bitmap must
 * never silently broaden a free-space scan to include allocated files. */
unsigned int catalog_free_space(disk_t *disk, alloc_data_t *space)
{
  unsigned char boot[512];
  if(disk->pread(disk,boot,sizeof(boot),0)!=sizeof(boot) || memcmp(boot+3,"NTFS    ",8)) exit(7);
  unsigned int sector=boot[11]+256U*boot[12], cluster=sector*boot[13];
  uint64_t sectors;
  memcpy(&sectors,boot+40,8);
  if(sector<512 || sector>4096 || (sector&(sector-1)) || !cluster || (cluster&(cluster-1)) ||
     !sectors || sectors>disk->disk_real_size/sector) exit(7);
  uint64_t clusters=sectors/boot[13], start=0, lcn=0;
  int free_run=0;
  _setmode(_fileno(stdin),_O_BINARY);
  free_list_search_space(space);
  while(lcn<clusters) {
    int byte=getc(stdin);
    if(byte==EOF) exit(7);
    if((byte==0 && free_run) || (byte==255 && !free_run)) {
      lcn+=clusters-lcn<8?clusters-lcn:8;
      continue;
    }
    for(int bit=0;bit<8 && lcn<clusters;bit++,lcn++) {
      int available=!(byte&(1<<bit));
      if(available && !free_run) start=lcn;
      if(!available && free_run) {
        alloc_data_t *run=calloc(1,sizeof(*run));
        if(!run) exit(8);
        run->start=start*cluster; run->end=lcn*cluster-1; run->data=1;
        td_list_add_tail(&run->list,&space->list);
      }
      free_run=available;
    }
  }
  if(free_run) {
    alloc_data_t *run=calloc(1,sizeof(*run));
    if(!run) exit(8);
    run->start=start*cluster; run->end=clusters*cluster-1; run->data=1;
    td_list_add_tail(&run->list,&space->list);
  }
  /* Drain bitmap padding so the producer can terminate normally. */
  while(getc(stdin)!=EOF) {}
  return cluster;
}
