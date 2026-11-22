/* GPL-2.0-or-later. Included after PhotoRec's filegen declarations. */
FILE *catalog_begin(file_recovery_t *file, struct param_disk_struct *disk);
void catalog_append(const unsigned char *bytes, unsigned int count);
void catalog_emit(file_recovery_t *file);
unsigned int catalog_free_space(struct param_disk_struct *disk, alloc_data_t *space);
size_t catalog_read(void *out, size_t size, size_t count, FILE *stream);
int catalog_seek(FILE *stream, long long offset, int origin);
long long catalog_tell(FILE *stream);
int catalog_eof(FILE *stream);

#define fread catalog_read
#define fseek catalog_seek
#define ftell catalog_tell
#define ftello catalog_tell
#undef feof
#define feof catalog_eof
