SELECT jsonb_build_object('schema',n.nspname,'name',c.relname,'oid',c.oid::bigint,
      'kind',c.relkind::text,'owner',pg_get_userbyid(c.relowner),'acl',c.relacl::text,
      'options',c.reloptions,'rls',c.relrowsecurity,'partition',c.relispartition,
      'comment',obj_description(c.oid,'pg_class'),'persistence',c.relpersistence::text,
      'definition',CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,false) ELSE NULL END,
      'columns',(SELECT jsonb_agg(jsonb_build_object('name',a.attname,'type',a.atttypid::bigint,
        'mod',a.atttypmod,'null',a.attnotnull,'acl',a.attacl::text,'identity',a.attidentity::text,
        'default',pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum) FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),
      'constraints',(SELECT jsonb_agg(jsonb_build_object('name',k.conname,'def',pg_get_constraintdef(k.oid,false),
        'comment',obj_description(k.oid,'pg_constraint')) ORDER BY k.conname) FROM pg_constraint k WHERE k.conrelid=c.oid),
      'index',CASE WHEN c.relkind='i' THEN pg_get_indexdef(c.oid) ELSE NULL END,
      'sequence',(SELECT to_jsonb(s) FROM pg_sequence s WHERE s.seqrelid=c.oid))::text
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname=ANY($1) ORDER BY n.nspname,c.relname