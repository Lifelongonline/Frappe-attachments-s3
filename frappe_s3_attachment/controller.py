from __future__ import unicode_literals

import datetime
import os
import random
import re
import string

import boto3

from botocore.client import Config
from botocore.exceptions import ClientError

import frappe


import magic
from urllib.parse import quote, unquote, urlparse, parse_qs

# Marker used to detect a File.file_url that already points at our own S3
# streaming endpoint, i.e. the file has already been uploaded to S3 and is
# not a path on local disk.
S3_STREAM_URL_MARKER = "frappe_s3_attachment.controller.generate_file"

# Extensions that browsers can render/execute (HTML, SVG, XML markup can
# carry embedded <script>). These are always forced to download, regardless
# of what the detected mime type says - relying on mime-type sniffing alone
# is not a safe boundary against crafted or ambiguous files.
INLINE_DENYLIST_EXTENSIONS = {".html", ".htm", ".xhtml", ".svg", ".xml"}

# Extensions considered safe to preview inline in the browser.
INLINE_ALLOWLIST_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".pdf",
    ".mp3", ".wav", ".ogg",
    ".mp4", ".webm", ".mov",
    ".txt",
}


def get_s3_key_from_file_url(file_url):
    """
    If file_url already points to our S3 streaming endpoint, extract the
    underlying S3 object key from it.

    This happens when a File record is copied over without going through
    file_upload_to_s3 again first - e.g. when a document is cancelled and
    amended, frappe.model.document.copy_attachments_from_amended_from()
    creates a new File record re-using the old file_url as-is and calls
    doc.save(), which fires this app's after_insert hook a second time.
    In that case file_url is our /api/method/...generate_file redirect,
    not a path on local disk, so it must not be re-uploaded.
    """
    if not file_url or S3_STREAM_URL_MARKER not in file_url:
        return None
    query = parse_qs(urlparse(file_url).query)
    keys = query.get("key")
    return unquote(keys[0]) if keys else None


def get_content_disposition_type(file_name):
    """
    Decide whether a file should be previewed inline in the browser or
    forced to download as an attachment, based on its extension.
    """
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext in INLINE_DENYLIST_EXTENSIONS:
        return "attachment"
    if ext in INLINE_ALLOWLIST_EXTENSIONS:
        return "inline"
    return "attachment"


class S3Operations(object):

    def __init__(self):
        """
        Function to initialise the aws settings from frappe S3 File attachment
        doctype.
        """
        self.s3_settings_doc = frappe.get_doc(
            'S3 File Attachment',
            'S3 File Attachment',
        )
        if (
            self.s3_settings_doc.aws_key and
            self.s3_settings_doc.aws_secret
        ):
            self.S3_CLIENT = boto3.client(
                's3',
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4')
            )
        else:
            self.S3_CLIENT = boto3.client(
                's3',
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4')
            )
        self.BUCKET = self.s3_settings_doc.bucket_name
        self.folder_name = self.s3_settings_doc.folder_name

    def strip_special_chars(self, file_name):
        """
        Strips file charachters which doesnt match the regex.
        """
        regex = re.compile('[^0-9a-zA-Z._-]')
        file_name = regex.sub('', file_name)
        return file_name

    def key_generator(self, file_name, parent_doctype, parent_name):
        """
        Generate keys for s3 objects uploaded with file name attached.
        """
        hook_cmd = frappe.get_hooks().get("s3_key_generator")
        if hook_cmd:
            try:
                k = frappe.get_attr(hook_cmd[0])(
                    file_name=file_name,
                    parent_doctype=parent_doctype,
                    parent_name=parent_name
                )
                if k:
                    return k.rstrip('/').lstrip('/')
            except:
                pass

        file_name = file_name.replace(' ', '_')
        file_name = self.strip_special_chars(file_name)
        key = ''.join(
            random.choice(
                string.ascii_uppercase + string.digits) for _ in range(8)
        )

        today = datetime.datetime.now()
        year = today.strftime("%Y")
        month = today.strftime("%m")
        day = today.strftime("%d")

        doc_path = None

        if not doc_path:
            if self.folder_name:
                final_key = self.folder_name + "/" + year + "/" + month + \
                    "/" + day + "/" + parent_doctype + "/" + key + "_" + \
                    file_name
            else:
                final_key = year + "/" + month + "/" + day + "/" + \
                    parent_doctype + "/" + key + "_" + file_name
            return final_key
        else:
            final_key = doc_path + '/' + key + "_" + file_name
            return final_key

    def upload_files_to_s3_with_key(
            self, file_path, file_name, is_private, parent_doctype, parent_name
    ):
        """
        Uploads a new file to S3.
        Strips the file extension to set the content_type in metadata.
        """
        mime_type = magic.from_file(file_path, mime=True)
        key = self.key_generator(file_name, parent_doctype, parent_name)
        content_type = mime_type
        try:
            self.S3_CLIENT.upload_file(
                file_path, self.BUCKET, key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "ContentType": content_type,
                        "file_name": quote(file_name, safe='')
                    }
                }
            )

        except boto3.exceptions.S3UploadFailedError:
            frappe.log_error("File Upload Failed")
            frappe.throw(frappe._("File Upload Failed. Please try again."))
        return key

    def delete_from_s3(self, key):
        """Delete file from s3"""
        self.s3_settings_doc = frappe.get_doc(
            'S3 File Attachment',
            'S3 File Attachment',
        )

        if self.s3_settings_doc.delete_file_from_cloud:
            s3_client = boto3.client(
                's3',
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4')
            )

            try:
                s3_client.delete_object(
                    Bucket=self.s3_settings_doc.bucket_name,
                    Key=key
                )
            except ClientError:
                frappe.throw(frappe._("Access denied: Could not delete file"))

    def read_file_from_s3(self, key):
        """
        Function to read file from a s3 file.
        """
        return self.S3_CLIENT.get_object(Bucket=self.BUCKET, Key=key)

    def get_url(self, key, file_name=None):
        """
        Return url.

        :param bucket: s3 bucket name
        :param key: s3 object key
        """
        if self.s3_settings_doc.signed_url_expiry_time:
            self.signed_url_expiry_time = self.s3_settings_doc.signed_url_expiry_time # noqa
        else:
            self.signed_url_expiry_time = 120
        params = {
                'Bucket': self.BUCKET,
                'Key': key,

        }
        if file_name:
            disposition_type = get_content_disposition_type(file_name)
            params['ResponseContentDisposition'] = (
                "{0}; filename*=UTF-8''{1}".format(
                    disposition_type, quote(file_name, safe='')
                )
            )

        url = self.S3_CLIENT.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=self.signed_url_expiry_time,
        )

        return url


def get_ignored_s3_doctypes():
    """
    Doctypes whose File attachments must never be pushed to S3 - combines
    the "Doctype to Ignore S3 Attachments" list configured in this app with
    the ignore_s3_upload_for_doctype site-config list (defaults to
    ['Data Import']).
    """
    configured = frappe.db.get_all("Doctype to Ignore S3 Attachments", pluck="doc_type")
    from_config = frappe.local.conf.get('ignore_s3_upload_for_doctype') or ['Data Import']
    return set(configured) | set(from_config)


@frappe.whitelist()
def file_upload_to_s3(doc, method):
    """
    check and upload files to s3. the path check and
    """
    if doc.is_folder:
        return
    if (doc.attached_to_doctype or 'File') in get_ignored_s3_doctypes():
        return
    if not frappe.db.get_single_value("S3 File Attachment", "enable_s3_attachment"):
        return

    existing_key = get_s3_key_from_file_url(doc.file_url)
    if existing_key:
        # Already uploaded to S3 (e.g. this File record was copied over by
        # copy_attachments_from_amended_from on cancel + amend). There is no
        # local file to upload - just keep content_hash pointing at the
        # existing S3 key so deletion/cleanup keeps working.
        if doc.content_hash != existing_key:
            frappe.db.set_value("File", doc.name, "content_hash", existing_key)
            doc.content_hash = existing_key
        return

    s3_upload = S3Operations()
    path = doc.file_url
    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype or 'File'
    parent_name = doc.attached_to_name

    if not doc.is_private:
        file_path = site_path + '/public' + path
    else:
        file_path = site_path + path

    original_content_hash = doc.content_hash

    key = s3_upload.upload_files_to_s3_with_key(
        file_path, doc.file_name,
        doc.is_private, parent_doctype,
        parent_name
    )

    method = "frappe_s3_attachment.controller.generate_file"
    file_url = "/api/method/{0}?key={1}&file_name={2}".format(
        method, key, doc.file_name
    )

    frappe.db.sql("""UPDATE `tabFile` SET file_url=%s, folder=%s,
        old_parent=%s, content_hash=%s WHERE name=%s""", (
        file_url, 'Home/Attachments', 'Home/Attachments', key, doc.name))

    doc.file_url = file_url

    if parent_doctype and frappe.get_meta(parent_doctype).get('image_field'):
        frappe.db.set_value(parent_doctype, parent_name, frappe.get_meta(parent_doctype).get('image_field'), file_url)

    sync_attached_field_url(doc, file_url, path)

    # Another File record can point at this exact same physical file
    # (the same content attached to a different document) - resolve
    # every such not-yet-migrated sibling now, in this same pass,
    # since content_hash is about to be overwritten with the S3 key
    # and this is the only moment their shared identity is still
    # visible. See upload_existing_files_s3 for the full rationale.
    # Siblings belonging to an ignored doctype are skipped - their local
    # file must survive untouched, exactly as if they were never involved.
    if original_content_hash:
        ignored_doctypes = get_ignored_s3_doctypes()
        siblings = frappe.get_all("File", filters={
            "name": ["!=", doc.name],
            "content_hash": original_content_hash,
            "file_url": ["not like", f"%{S3_STREAM_URL_MARKER}%"],
        }, fields=["name", "attached_to_doctype", "file_url"])
        for sibling in siblings:
            if (sibling.attached_to_doctype or 'File') in ignored_doctypes:
                continue
            sibling_doc = frappe.get_doc("File", sibling.name)
            frappe.db.sql(
                "UPDATE `tabFile` SET file_url=%s, content_hash=%s WHERE name=%s",
                (file_url, key, sibling.name)
            )
            sync_attached_field_url(sibling_doc, file_url, sibling.file_url)

    if not has_other_pending_reference(doc, path):
        os.remove(file_path)



@frappe.whitelist()
def generate_file(key=None, file_name=None):
    """
    Function to stream file from s3.
    """
    if key:
        s3_upload = S3Operations()
        signed_url = s3_upload.get_url(key, file_name)
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = signed_url
    else:
        frappe.local.response['body'] = "Key not found."
    return


def _matches_source_file(current_value, original_file_url, file_name):
    """
    True if current_value (a stale Attach-field value) genuinely refers to
    the file being migrated. Matched primarily by exact equality against
    its own pre-migration file_url (the strongest signal - two different
    files essentially never share the exact same local path+name unless
    they really are the same physical file); falls back to an exact match
    on the trailing filename only when original_file_url isn't available.

    Deliberately an exact match, never a substring: a file named "1.jpg"
    must not match a value ending in "21.jpg".
    """
    if not current_value or S3_STREAM_URL_MARKER in current_value:
        return False
    if original_file_url:
        return current_value == original_file_url
    return current_value.rsplit("/", 1)[-1] == file_name


def _write_if_unclaimed(doctype, name, fieldname, current_value, original_file_url, file_name, file_url):
    """
    Write file_url onto doctype/name.fieldname only if that field isn't
    already claimed by a *different* file. A blank field is free to claim.
    A field already holding this exact file (by original_file_url or
    filename) is claimed by us and safe to overwrite with the new URL. A
    field holding something else entirely is left alone - two File records
    can end up recorded against the very same doctype/name/fieldname (e.g.
    a field was re-attached and the old File record was never cleaned up),
    and silently picking whichever gets processed last would let a stale
    file clobber a field that's actually showing a different, current one.
    """
    if current_value and not _matches_source_file(current_value, original_file_url, file_name):
        frappe.log_error(
            "S3 attachment field conflict",
            f"File for '{file_name}' claims {doctype}/{name}.{fieldname}, but that field "
            f"already holds a different value ({current_value!r}) not matching this file. "
            f"Left untouched to avoid overwriting a possibly-current attachment with a stale one."
        )
        return False
    frappe.db.set_value(doctype, name, fieldname, file_url, update_modified=False)
    return True


def sync_attached_field_url(doc, file_url, original_file_url=None):
    """
    Propagate the migrated S3 URL onto whatever field actually displays
    this file, after the File document itself has been updated.

    attached_to_name is not always the parent document's own name: a file
    attached via a grid row's Attach control can have attached_to_doctype
    left as the top-level doctype (e.g. Shipment) while attached_to_name
    is the *child row's* own name. frappe.db.exists(attached_to_doctype,
    attached_to_name) is False in that case, and the field being synced
    may also only exist on that child doctype, not the parent.

    original_file_url is the file's own pre-migration local file_url, used
    to disambiguate when the same doctype/name/fieldname is claimed by
    more than one File record - pass it whenever the caller still has it
    (it may already be gone from doc.file_url by the time this runs).
    """
    parent_doctype = doc.attached_to_doctype
    parent_name = doc.attached_to_name
    fieldname = doc.attached_to_field
    file_name = doc.file_name

    if not (parent_doctype and parent_name):
        return

    if frappe.db.exists(parent_doctype, parent_name):
        # attached_to_name really is the parent document.
        meta = frappe.get_meta(parent_doctype)
        if fieldname and meta.has_field(fieldname):
            current_value = frappe.db.get_value(parent_doctype, parent_name, fieldname)
            _write_if_unclaimed(parent_doctype, parent_name, fieldname, current_value,
                                 original_file_url, file_name, file_url)
            return
        # fieldname is blank, or it names a field that only exists on one
        # of the parent's own child tables - search those, scoped to
        # parent_name, never site-wide.
        _sync_via_child_tables(meta, parent_name, fieldname, file_name, original_file_url, file_url)
        return

    # Not a real top-level doc - attached_to_name is likely the name of a
    # row inside one of parent_doctype's own child tables instead.
    resolved = _find_child_row(parent_doctype, parent_name)
    if not resolved:
        return
    child_doctype, row_name = resolved

    child_meta = frappe.get_meta(child_doctype)
    if fieldname and child_meta.has_field(fieldname):
        current_value = frappe.db.get_value(child_doctype, row_name, fieldname)
        _write_if_unclaimed(child_doctype, row_name, fieldname, current_value,
                             original_file_url, file_name, file_url)
        return

    # fieldname is blank - we already know exactly which row this is, so
    # just scan its own Attach fields directly, no further search needed.
    row = frappe.db.get_value(child_doctype, row_name, "*", as_dict=True)
    for df in child_meta.fields:
        if df.fieldtype != "Attach":
            continue
        current_value = row.get(df.fieldname)
        if _matches_source_file(current_value, original_file_url, file_name):
            frappe.db.set_value(child_doctype, row_name, df.fieldname, file_url, update_modified=False)
            return


def _find_child_row(parent_doctype, row_name):
    """If row_name is a row in one of parent_doctype's child tables, return
    (child_doctype, row_name). Child row names are unique across the site."""
    for table_df in frappe.get_meta(parent_doctype).get_table_fields():
        if frappe.db.exists(table_df.options, row_name):
            return table_df.options, row_name
    return None


def _sync_via_child_tables(meta, parent_name, fieldname, file_name, original_file_url, file_url):
    for table_df in meta.get_table_fields():
        child_meta = frappe.get_meta(table_df.options)
        candidate_fields = (
            [fieldname] if fieldname and child_meta.has_field(fieldname)
            else [df.fieldname for df in child_meta.fields if df.fieldtype == "Attach"]
        )
        if not candidate_fields:
            continue
        for row in frappe.get_all(table_df.options, filters={"parent": parent_name},
                                   fields=["name"] + candidate_fields):
            for cf in candidate_fields:
                current_value = row.get(cf)
                if _matches_source_file(current_value, original_file_url, file_name):
                    frappe.db.set_value(table_df.options, row.name, cf, file_url, update_modified=False)
                    return


def has_other_pending_reference(doc, local_file_url):
    """
    True if some other, not-yet-migrated File record still points at this
    exact local file_url - deleting the physical file now would break that
    record's own migration when its turn comes.
    """
    return bool(frappe.db.exists("File", {
        "name": ["!=", doc.name],
        "file_url": local_file_url,
        "is_folder": 0,
    }))


def upload_existing_files_s3(name, file_name):
    """
    Function to upload all existing files.

    Two File records can share the exact same physical file on disk (the
    same content attached to two different documents, e.g. one PDF linked
    from both a Purchase Order and a Purchase Receipt). content_hash holds
    the real content hash only until the first of them is migrated - the
    UPDATE below overwrites it with the S3 key, since delete_from_s3() and
    the URL-rebuild path both depend on content_hash holding the key from
    that point on. So the original hash can only ever be compared *once*,
    right here, before it's overwritten - there's no way to defer this
    check to each sibling's own turn, because by then the signal is gone.
    That's why every not-yet-migrated sibling sharing this hash is
    resolved and updated in this same pass, instead of re-uploading the
    same bytes N times and hoping a later pass can still tell they match.
    """
    file_doc_name = frappe.db.get_value('File', {'name': name})
    if not file_doc_name:
        return

    doc = frappe.get_doc('File', name)

    if (doc.attached_to_doctype or 'File') in get_ignored_s3_doctypes():
        return

    # Already migrated - e.g. this File was resolved as a sibling below
    # during an earlier call in the same batch. Nothing left to upload.
    existing_key = get_s3_key_from_file_url(doc.file_url)
    if existing_key:
        return

    s3_upload = S3Operations()
    path = doc.file_url
    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype
    parent_name = doc.attached_to_name
    if not doc.is_private:
        file_path = site_path + '/public' + path
    else:
        file_path = site_path + path

    original_content_hash = doc.content_hash

    key = s3_upload.upload_files_to_s3_with_key(
        file_path, doc.file_name,
        doc.is_private, parent_doctype,
        parent_name
    )

    method = "frappe_s3_attachment.controller.generate_file"
    file_url = "/api/method/{0}?key={1}&file_name={2}".format(
        method, key, file_name
    )

    frappe.db.sql("""UPDATE `tabFile` SET file_url=%s, folder=%s,
        old_parent=%s, content_hash=%s WHERE name=%s""", (
        file_url, 'Home/Attachments', 'Home/Attachments', key, doc.name))
    sync_attached_field_url(doc, file_url, path)

    if original_content_hash:
        ignored_doctypes = get_ignored_s3_doctypes()
        siblings = frappe.get_all("File", filters={
            "name": ["!=", doc.name],
            "content_hash": original_content_hash,
            "file_url": ["not like", f"%{S3_STREAM_URL_MARKER}%"],
        }, fields=["name", "attached_to_doctype", "file_url"])
        for sibling in siblings:
            if (sibling.attached_to_doctype or 'File') in ignored_doctypes:
                continue
            sibling_doc = frappe.get_doc("File", sibling.name)
            frappe.db.sql(
                "UPDATE `tabFile` SET file_url=%s, content_hash=%s WHERE name=%s",
                (file_url, key, sibling.name)
            )
            sync_attached_field_url(sibling_doc, file_url, sibling.file_url)

    if not has_other_pending_reference(doc, path):
        os.remove(file_path)

    frappe.db.commit()


def s3_file_regex_match(file_url):
    """
    Match the public file regex match.
    """
    return re.match(
        r'^(https:|/api/method/frappe_s3_attachment.controller.generate_file)',
        file_url
    )


@frappe.whitelist()
def migrate_existing_files():
    """
    Function to migrate the existing files to s3.

    Files pending migration are split into batches and each batch is
    enqueued as a separate background job, instead of migrating every
    file synchronously within the request.
    """
    # get_all_files_from_public_folder_and_upload_to_s3
    batch_size = 1000
    files_list = frappe.get_all(
        'File',
        fields=['name', 'file_url', 'file_name']
    )
    files_to_migrate = [
        file for file in files_list
        if file['file_url'] and not s3_file_regex_match(file['file_url'])
    ]

    for index in range(0, len(files_to_migrate), batch_size):
        batch = files_to_migrate[index:index + batch_size]
        frappe.enqueue(
            method=migrate_files_batch,
            queue='long',
            timeout=25000,
            job_name='s3_migrate_batch_{0}'.format(index // batch_size),
            files=batch,
        )
    return True


def migrate_files_batch(files):
    """
    Migrate a batch of files to s3.

    Runs as a background job, enqueued in chunks of `batch_size` by
    `migrate_existing_files`.
    """
    for file in files:
        try:
            upload_existing_files_s3(file['name'], file['file_name'])
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()


@frappe.whitelist()
def fix_double_encoded_file_urls():
    """
    Rebuild file_url for File records that were saved with the S3 key /
    file name already percent-encoded (a since-reverted change caused
    Frappe's own link rendering to quote them a second time, e.g.
    '%20' -> '%2520', '%2F' -> '%252F').

    Rebuilds file_url from content_hash (the real, raw S3 key - never
    touched by the bug) and file_name (the raw filename), matching
    what the controller now saves for new uploads. Safe to run
    repeatedly - records already in the correct form are left alone.

    Batched the same way as `migrate_existing_files`, since this can
    run over the whole File table.
    """
    batch_size = 1000
    files = frappe.get_all(
        'File',
        filters={'file_url': ['like', '/api/method/frappe_s3_attachment.controller.generate_file?key=%']},
        fields=['name', 'file_url', 'file_name', 'content_hash']
    )

    for index in range(0, len(files), batch_size):
        batch = files[index:index + batch_size]
        frappe.enqueue(
            method=fix_file_urls_batch,
            queue='long',
            timeout=25000,
            job_name='s3_fix_file_url_batch_{0}'.format(index // batch_size),
            files=batch,
        )
    return True


def fix_file_urls_batch(files):
    """
    Rebuild file_url for a batch of File records.

    Runs as a background job, enqueued in chunks of `batch_size` by
    `fix_double_encoded_file_urls`.
    """
    method = "frappe_s3_attachment.controller.generate_file"
    for file in files:
        try:
            key = file.get('content_hash')
            if not key:
                continue
            correct_url = "/api/method/{0}?key={1}&file_name={2}".format(
                method, key, file.get('file_name') or ''
            )
            if file.get('file_url') != correct_url:
                frappe.db.set_value(
                    'File', file['name'], 'file_url', correct_url,
                    update_modified=False
                )
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()


def delete_from_cloud(doc, method):
    """Delete file from s3"""
    if doc.attached_to_doctype in frappe.db.get_all("Doctype to Ignore S3 Attachments", pluck="doc_type"):
        return
    if not frappe.db.get_single_value("S3 File Attachment", "enable_s3_attachment"):
        return
    s3 = S3Operations()
    s3.delete_from_s3(doc.content_hash)


@frappe.whitelist()
def ping():
    """
    Test function to check if api function work.
    """
    return "pong"
