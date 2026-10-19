
    #[test]
    fn safe_promote_pg_generated_inventory_keeps_legacy_objects_byte_identical() {
        let Some(base)=endpoint() else { return };
        let (original,candidate,_,dir)=fixture(&base);
        let schemas=vec![original.schema.clone().unwrap()];
        let mut client=connect(&original).unwrap();
        let legacy=client.query(include_str!("../fixtures/postgres_legacy_objects.sql"),&[&schemas]).unwrap().into_iter()
            .map(|row|serde_json::from_str::<Value>(row.get::<_,&str>(0)).unwrap()).collect::<Vec<_>>();
        assert_eq!(serde_json::to_vec(&objects(&mut client,&schemas).unwrap()).unwrap(),serde_json::to_vec(&legacy).unwrap());
        client.batch_execute("ALTER TABLE items ADD COLUMN g int GENERATED ALWAYS AS(id+1) STORED").unwrap();
        let changed=objects(&mut client,&schemas).unwrap();
        let table=changed.iter().find(|object|object["name"]=="items").unwrap();
        let columns=table["columns"].as_array().unwrap();
        assert_eq!(columns.last().unwrap()["generated"],"s");
        assert!(columns[..columns.len()-1].iter().all(|column|column.get("generated").is_none()));
        cleanup(&base,&original,&candidate,None,&dir);
    }
    #[test]
    fn safe_promote_pg_single_statement_ddl_stops_chains() {
        let Some(base)=endpoint() else { return };
        let (original,candidate,_,dir)=fixture(&base);
        let mut adapter=LiveAdapter::connect(&original).unwrap();
        let chain="CREATE TABLE extra(id int); DROP TABLE items";
        for err in [adapter.execute_single(chain).unwrap_err(),adapter.create_new_table(chain).unwrap_err()] {
            assert!(err.contains("cannot insert multiple commands"),"{err}");
        }
        let mut client=connect(&original).unwrap();
        assert!(client.query_one("SELECT count(*) FROM items",&[]).unwrap().get::<_,i64>(0)>0);
        assert!(client.query_one("SELECT to_regclass('extra')::text",&[]).unwrap().get::<_,Option<String>>(0).is_none());
        cleanup(&base,&original,&candidate,None,&dir);
    }
